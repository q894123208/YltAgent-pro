from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.core.config import SETTINGS
from app.core.process_logger import log_step
from app.schemas.chat import AgentTrace, Evidence
from app.services.medical_business import (
    DEPARTMENTS,
    DISCLAIMER,
    build_search_query,
    compliance_guard,
    mentions_child,
    normalize_department,
    normalize_risk,
    public_evidence,
    sanitize_internal_text,
    strip_meta_tag,
)
from app.services.chroma_rag_service import get_chroma_service
from app.services.deep_search import web_search
from app.services.llm_client import LLMClient
from app.services.skills import HIGH_RISK_KEYWORDS, analyze_symptoms, assess_risk

MAX_FOLLOW_UPS = 4
COMPLETENESS_THRESHOLD = 0.75

DURATION_PATTERN = re.compile(r"(\d+\s*(?:天|日|周|月|小时|钟头|分钟)|半年|几年|昨天|今天|前天|一周|两周|三天)")
SEVERITY_PATTERN = re.compile(r"(轻微|明显|剧烈|加重|缓解|持续|反复|偶尔|越来越|突然)")


class GuidedTriageState(TypedDict, total=False):
    latest_message: str
    patient: Dict[str, Any]
    history: List[Dict[str, Any]]
    user_id: str
    symptoms: List[str]
    duration: str
    severity: str
    associated_symptoms: List[str]
    red_flags: List[str]
    completeness: float
    missing_fields: List[str]
    questions_asked: int
    next_action: Literal["followup", "conclude"]
    follow_up: str
    answer: str
    risk_level: str
    recommended_department: str
    evidence: List[Dict[str, Any]]
    trace: List[Dict[str, Any]]
    phase: Literal["collecting", "completed"]


def _trace(agent: str, action: str, detail: str, trace: List[Dict[str, Any]]) -> None:
    trace.append(AgentTrace(agent=agent, action=action, detail=detail).model_dump())
    log_step("guided_triage.trace", agent=agent, action=action, detail=detail)


def _merge_unique(existing: List[str], items: List[str]) -> List[str]:
    seen = {x.lower() for x in existing}
    merged = list(existing)
    for item in items:
        token = str(item).strip()
        if token and token.lower() not in seen:
            merged.append(token)
            seen.add(token.lower())
    return merged


def _extract_duration(text: str) -> str:
    match = DURATION_PATTERN.search(text or "")
    return match.group(0).strip() if match else ""


def _extract_severity(text: str) -> str:
    match = SEVERITY_PATTERN.search(text or "")
    return match.group(0).strip() if match else ""


def _collect_conversation_text(state: GuidedTriageState) -> str:
    parts = [str(state.get("latest_message") or "")]
    for item in state.get("history") or []:
        if item.get("role") == "user" and item.get("content"):
            parts.append(str(item["content"]))
    return "\n".join(parts)


def _compute_missing_fields(state: GuidedTriageState) -> List[str]:
    missing: List[str] = []
    if not state.get("symptoms"):
        missing.append("symptoms")
    if not state.get("duration"):
        missing.append("duration")
    if not state.get("severity"):
        missing.append("severity")
    if not state.get("associated_symptoms"):
        missing.append("associated_symptoms")
    return missing


def _compute_completeness(state: GuidedTriageState) -> float:
    score = 0.0
    if state.get("symptoms"):
        score += 0.35
    if state.get("duration"):
        score += 0.25
    if state.get("severity"):
        score += 0.2
    if state.get("associated_symptoms"):
        score += 0.2
    return round(min(score, 1.0), 2)


def ingest_node(state: GuidedTriageState) -> Dict[str, Any]:
    trace = list(state.get("trace") or [])
    text = _collect_conversation_text(state)
    symptom_info = analyze_symptoms(text)
    symptoms = _merge_unique(list(state.get("symptoms") or []), symptom_info.get("symptoms") or [])
    associated = _merge_unique(list(state.get("associated_symptoms") or []), symptoms[1:] if len(symptoms) > 1 else [])
    if symptoms:
        associated = _merge_unique(associated, symptoms[1:])
    duration = state.get("duration") or _extract_duration(text)
    severity = state.get("severity") or _extract_severity(text)
    red_flags = [kw for kw in HIGH_RISK_KEYWORDS if kw in text]
    _trace("CollectAgent", "extract", f"已整理症状线索 {len(symptoms)} 条，红旗 {len(red_flags)} 项", trace)
    merged = {
        **state,
        "symptoms": symptoms[:8],
        "associated_symptoms": associated[:8],
        "duration": duration,
        "severity": severity,
        "red_flags": red_flags,
        "trace": trace,
    }
    merged["missing_fields"] = _compute_missing_fields(merged)
    merged["completeness"] = _compute_completeness(merged)
    return merged


def assess_node(state: GuidedTriageState) -> Dict[str, Any]:
    trace = list(state.get("trace") or [])
    completeness = float(state.get("completeness") or 0)
    questions_asked = int(state.get("questions_asked") or 0)
    red_flags = state.get("red_flags") or []
    missing = state.get("missing_fields") or _compute_missing_fields(state)

    if red_flags:
        next_action: Literal["followup", "conclude"] = "conclude"
        detail = f"识别到高危信号（{', '.join(red_flags[:3])}），直接进入分诊结论"
    elif completeness >= COMPLETENESS_THRESHOLD or questions_asked >= MAX_FOLLOW_UPS:
        next_action = "conclude"
        detail = f"信息完整度 {completeness:.0%}，开始生成分诊结论"
    else:
        next_action = "followup"
        detail = f"信息完整度 {completeness:.0%}，仍需补充：{', '.join(missing)}"

    _trace("RouterAgent", "assess", detail, trace)
    return {**state, "next_action": next_action, "missing_fields": missing, "trace": trace}


def route_after_assess(state: GuidedTriageState) -> Literal["followup", "retrieve"]:
    return "followup" if state.get("next_action") == "followup" else "retrieve"


FOLLOWUP_TEMPLATES = {
    "symptoms": "为了更准确判断科室，请先告诉我：您现在最不舒服的主要症状是什么？",
    "duration": "这些症状大概持续了多久？例如几小时、几天或几周。",
    "severity": "目前症状是轻微、明显还是比较严重？最近是在加重、缓解还是差不多？",
    "associated_symptoms": "除了主诉之外，还有没有发热、咳嗽、胸痛、呕吐、皮疹等伴随表现？",
}


async def followup_node(state: GuidedTriageState) -> Dict[str, Any]:
    trace = list(state.get("trace") or [])
    llm = LLMClient()
    missing = state.get("missing_fields") or ["symptoms"]
    target = missing[0]
    questions_asked = int(state.get("questions_asked") or 0) + 1
    follow_up = FOLLOWUP_TEMPLATES.get(target, FOLLOWUP_TEMPLATES["symptoms"])

    if llm.enabled:
        system = (
            "你是互联网医院分步导诊助手。当前处于信息收集阶段，只能提出一个简短追问，"
            "不要给出科室推荐、诊断或用药建议。语气温和，50字以内。"
        )
        user = f"""
已收集信息：
- 症状：{', '.join(state.get('symptoms') or []) or '未知'}
- 持续时间：{state.get('duration') or '未知'}
- 严重程度：{state.get('severity') or '未知'}
- 伴随症状：{', '.join(state.get('associated_symptoms') or []) or '未知'}
- 患者：{json.dumps(state.get('patient') or {}, ensure_ascii=False)}

本轮优先补充字段：{target}
请围绕该字段提出一个追问。
"""
        try:
            raw = await llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                timeout=20.0,
            )
            cleaned = sanitize_internal_text(raw.strip())
            if cleaned:
                follow_up = cleaned
        except Exception as exc:
            log_step("guided_triage.followup.llm_failed", error=type(exc).__name__)

    _trace("FollowupAgent", "ask", f"第 {questions_asked} 轮追问：{target}", trace)
    return {
        **state,
        "questions_asked": questions_asked,
        "follow_up": follow_up,
        "answer": follow_up,
        "phase": "collecting",
        "trace": trace,
    }


async def retrieve_node(state: GuidedTriageState) -> Dict[str, Any]:
    trace = list(state.get("trace") or [])
    patient = state.get("patient") or {}
    history = state.get("history") or []
    message = state.get("latest_message") or ""
    query = build_search_query("triage", message, patient, history)
    evidence: List[Evidence] = []

    chroma = get_chroma_service()
    if chroma.available and SETTINGS.get("rag", {}).get("use_chroma", True):
        kb_evidence = await chroma.query_kb(query)
        evidence.extend(kb_evidence)
        _trace("RAGAgent", "retrieve", f"Chroma 知识库召回 {len(kb_evidence)} 条", trace)
    web_evidence = await web_search(query, limit=2, timeout=6.0)
    evidence.extend(web_evidence)
    _trace("ResearchAgent", "deep_search", f"联网搜索返回 {len(web_evidence)} 条", trace)

    return {
        **state,
        "evidence": [public_evidence(item).model_dump() for item in evidence[:8]],
        "trace": trace,
    }


async def conclude_node(state: GuidedTriageState) -> Dict[str, Any]:
    trace = list(state.get("trace") or [])
    llm = LLMClient()
    patient = state.get("patient") or {}
    message = state.get("latest_message") or ""
    history = state.get("history") or []
    evidence_items = state.get("evidence") or []
    risk_hint = assess_risk(_collect_conversation_text(state))

    evidence_blob = "\n".join(
        f"- {item.get('title')}: {str(item.get('content', ''))[:220]}"
        for item in evidence_items[:6]
    )
    system = f"""你是互联网医院分步导诊助手。用户已完成结构化追问，请给出最终分诊建议。
要求：
1. 用自然中文总结症状与风险判断，不要确诊、不开处方。
2. 明确建议挂号科室，融入正文。
3. 如有红旗症状，明确提示及时线下就医或急诊。
4. 文末必须附加系统标签（用户不可见）：
<meta>{{"risk_level":"低风险|中风险|高风险","recommended_department":"科室名"}}</meta>
可选科室：{', '.join(DEPARTMENTS[:20])} 等。
{DISCLAIMER}
"""
    user = f"""
患者信息：{json.dumps(patient, ensure_ascii=False)}
主诉与追问汇总：
- 症状：{', '.join(state.get('symptoms') or [])}
- 持续时间：{state.get('duration') or '未说明'}
- 严重程度：{state.get('severity') or '未说明'}
- 伴随症状：{', '.join(state.get('associated_symptoms') or [])}
- 红旗信号：{', '.join(state.get('red_flags') or []) or '暂无'}
最近用户描述：{message}
历史对话片段：{json.dumps([h.get('content', '')[:120] for h in history[-4:]], ensure_ascii=False)}
检索证据：
{evidence_blob or '无'}
本地风险提示：{risk_hint.get('risk_level')}
"""
    answer = ""
    if llm.enabled:
        try:
            answer = await llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                timeout=60.0,
            )
        except Exception as exc:
            log_step("guided_triage.conclude.llm_failed", error=type(exc).__name__)

    if not answer.strip():
        dept = normalize_department(risk_hint.get("department"), "triage")
        answer = (
            f"根据您描述的症状，当前评估为{risk_hint.get('risk_level', '中风险')}。"
            f"建议优先考虑挂号{dept}进一步评估。"
            f"\n\n{DISCLAIMER}"
            f'\n<meta>{{"risk_level":"{risk_hint.get("risk_level", "中风险")}","recommended_department":"{dept}"}}</meta>'
        )

    cleaned, meta = strip_meta_tag(answer)
    risk_level = normalize_risk(meta.get("risk_level") or risk_hint.get("risk_level"))
    department = normalize_department(meta.get("recommended_department"), "triage")
    if mentions_child(_collect_conversation_text(state), patient):
        department = "儿科"
    cleaned = sanitize_internal_text(compliance_guard(cleaned))
    _trace("ReasoningAgent", "conclude", f"分诊完成：{risk_level} / {department}", trace)

    return {
        **state,
        "answer": cleaned,
        "risk_level": risk_level,
        "recommended_department": department,
        "phase": "completed",
        "trace": trace,
    }


def build_guided_triage_graph():
    graph = StateGraph(GuidedTriageState)
    graph.add_node("ingest", ingest_node)
    graph.add_node("assess", assess_node)
    graph.add_node("followup", followup_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("conclude", conclude_node)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "assess")
    graph.add_conditional_edges("assess", route_after_assess, {"followup": "followup", "retrieve": "retrieve"})
    graph.add_edge("followup", END)
    graph.add_edge("retrieve", "conclude")
    graph.add_edge("conclude", END)
    return graph.compile(checkpointer=MemorySaver())


_GUIDED_GRAPH = None


def get_guided_triage_graph():
    global _GUIDED_GRAPH
    if _GUIDED_GRAPH is None:
        _GUIDED_GRAPH = build_guided_triage_graph()
    return _GUIDED_GRAPH
