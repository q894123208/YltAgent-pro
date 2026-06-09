from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from app.core.config import SETTINGS
from app.core.process_logger import log_step
from app.core.database import (
    add_encounter,
    add_message,
    appointment_key,
    attach_documents_to_message,
    get_medical_document,
    get_documents_by_ids,
    list_session_medical_documents,
    list_medical_documents,
    list_messages,
    upsert_session,
)
from app.schemas.chat import AgentTrace, ChatRequest, Evidence
from app.services.chroma_rag_service import get_chroma_service
from app.services.deep_search import web_search
from app.services.llm_client import LLMClient
from app.services.rag_service import RAGService
from app.services.skills import analyze_symptoms, assess_risk, compliance_guard, lifestyle_recommendations
from app.services.vlm_service import build_report_summary


DISCLAIMER = "以上内容仅用于健康科普、预问诊和就医参考，不能替代医生诊断、处方或治疗。"


def load_department_aliases() -> Dict[str, List[str]]:
    kb_path = Path(SETTINGS.get("rag", {}).get("triage_kb_dir", "../data/medical-triage-kb"))
    if not kb_path.is_absolute():
        kb_path = (Path(__file__).resolve().parents[3] / kb_path).resolve()
    alias_file = kb_path / "metadata" / "department_aliases.json"
    if not alias_file.exists():
        return {}
    try:
        data = json.loads(alias_file.read_text(encoding="utf-8"))
        return {str(k): [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
    except Exception:
        return {}


DEPARTMENT_ALIASES = load_department_aliases()
DEPARTMENTS = list(DEPARTMENT_ALIASES.keys()) or [
    "呼吸内科",
    "消化内科",
    "心血管内科",
    "神经内科",
    "内分泌科",
    "肾内科",
    "普通内科",
    "普外科",
    "骨科",
    "泌尿外科",
    "妇科",
    "产科",
    "儿科",
    "眼科",
    "耳鼻喉科",
    "口腔科",
    "皮肤科",
    "精神心理科",
    "急诊科",
    "发热门诊",
]
if "药学门诊" not in DEPARTMENTS:
    DEPARTMENTS.append("药学门诊")

SCENE_NAMES = {
    "triage": "智能分诊",
    "consultation": "线上问诊",
    "medication": "用药咨询",
}


# =========================================================
# Prompt 构造（自然语言 + 末尾 <meta> 标签）
# =========================================================

def build_natural_system_prompt(scene: str) -> str:
    if scene == "triage":
        focus = "重点是初步分诊：综合主诉、伴随症状、患者基础情况，判断风险等级和推荐挂号科室。"
    elif scene == "medication":
        focus = "重点是用药安全咨询：关注药物相互作用、禁忌、特殊人群（孕妇/儿童/老人）、合并用药风险与就医边界。"
    else:
        focus = "重点是健康科普问诊：做症状梳理、可能疾病分析与生活建议。"

    return f"""你是互联网医院的医疗助手 AI。{focus}请用自然、温和、清晰的中文回答，像医生面诊那样有温度，禁止确诊、禁止开处方。

回答前请先在心里完成任务判断：用户是在普通问答、解读本轮报告、结合历史资料分析、做纵向对比，还是只做分诊。不要把这个判断过程输出给用户。

证据使用原则：
1. 本轮上传的报告/影像代表“当前证据”，优先级最高。
2. 历史报告、历史问诊、知识库和联网资料只能作为背景、解释或变化对比，不能覆盖本轮报告。
3. 当当前证据与历史资料不一致时，以当前证据描述当前状态；历史资料只能表述为“既往/过去/上次曾提示”。
4. 如果当前报告对某项发现、指标或结论写明“未见、阴性、正常、未提示、较前消失/减轻”，禁止把历史资料中的同类异常说成当前仍存在。
5. 若没有高相关历史报告，就直接说明主要基于本轮资料分析，不要强行对比。

回答的内容顺序：
1. 如果用户上传了报告/影像，先解读本轮报告的关键信息，明确异常项、正常项和临床意义。
2. 如果系统提供了相关历史报告，再说明它们与本轮资料的关系：新增、消失、减轻、加重、稳定或资料不足；不要混淆当前与既往。
3. 结合患者基础情况和当前描述分析症状。
4. 给出 2-4 种可能的疾病或原因，配可能性高低的判断思路，用"考虑"、"可能"、"不排除"这样的措辞。
5. 在叙述中自然给出你判断的**风险等级**（低风险 / 中风险 / 高风险），并说明判断理由。
6. 自然地推荐患者下一步去哪个科室就诊（用"建议挂号 XX 科"这样的口吻，融入正文，不要做成单独的标题分段）。
7. 提供生活护理 / 自我观察 / 是否需要就医的建议。
8. 如果存在红旗症状（如剧烈胸痛、呼吸困难、大量出血、意识改变、持续高热不退等），明确警示需要及时线下就医或急诊。
9. 如果还有信息不清楚的地方，最后追问 0-4 个问题；如果信息已经够充分就不问。

风格要求：
- 自然有温度的医生口吻，不要用"结论："、"依据与原因："、"风险等级："、"推荐科室："这类刻板的固定标题分段。
- 可以使用 **加粗** 强调关键词、使用编号或破折号列表、使用换行分段，但禁止 JSON / 代码块。
- 不能确诊、不能开处方、不能给具体处方剂量。

回答正文写完后，**在最后一行单独**输出一个 meta 标签，前面不要写"meta"两个字，也不要写任何说明，直接换行后输出：
<meta>{{"risk_level":"低风险或中风险或高风险","recommended_department":"必须从下面列表选一个"}}</meta>

推荐科室必须从下面这个列表里精确选择一个：
{', '.join(DEPARTMENTS)}

如果用户描述对象是小孩、儿童、宝宝、婴幼儿，或患者年龄小于 14 岁，分诊场景必须优先选择「儿科」。
用药咨询场景默认选「药学门诊」。meta 标签是给系统使用的，用户不会看到，但你必须输出。
"""


def build_natural_user_prompt(
    scene: str,
    message: str,
    patient: Dict[str, Any],
    history: List[Dict[str, Any]],
    evidence: List[Evidence],
    attachment_blob: str = "",
    session_report_blob: str = "",
    intent_plan: Dict[str, Any] | None = None,
) -> str:
    parts: List[str] = [f"业务场景：{SCENE_NAMES.get(scene, scene)}"]
    intent_plan = intent_plan or {}
    if intent_plan:
        parts.append(
            "【本轮任务意图与检索计划】\n"
            + json.dumps(
                {
                    "intent": intent_plan.get("intent"),
                    "use_current_attachments": intent_plan.get("use_current_attachments"),
                    "use_user_report_memory": intent_plan.get("use_user_report_memory"),
                    "memory_purpose": intent_plan.get("memory_purpose"),
                    "evidence_policy": intent_plan.get("evidence_policy"),
                    "reason": intent_plan.get("reason"),
                },
                ensure_ascii=False,
            )
        )
    if attachment_blob:
        parts.append(
            "【最高优先级：用户本轮上传的报告/影像】\n"
            "下面内容只代表本轮附件。若它和历史报告或旧对话冲突，必须以本轮附件为准；"
            "同时要把相关历史报告作为对比依据，说明哪些异常新增、消失、减轻或稳定；"
            "不能把历史报告中的异常写成本轮报告仍然存在。"
            "这个规则适用于所有指标、影像发现和检查结论，不限于某一种疾病或某一个部位。"
            "如果本轮报告明确写了“未见/阴性/正常/未提示/较前消失或减轻”，只能按当前报告描述当前状态；"
            "历史异常只能写成“既往曾有、当前未再显示/当前报告未提示”。\n"
            + attachment_blob
        )
    if session_report_blob:
        parts.append(
            "【本会话已上传的最近报告】\n"
            "下面是用户在本会话前面轮次已经上传并解析过的报告，可作为已知报告事实使用。"
            "如果用户当前追问“最新报告、刚才那份报告、现在为什么没有了、同一家医院”等，"
            "应直接引用这些已知报告，不要使用“如果最新报告……”这类假设语气。"
            "其中最新报告代表最近一次已知检查结果；历史报告只能作为既往对照。\n"
            + session_report_blob
        )
    if any(word in message for word in ["相比", "变化", "对比", "复查", "三个月前", "之前", "较前"]):
        parts.append(
            "【对比要求】\n"
            "用户在询问变化趋势。必须按报告类型和日期逐项对比，例如血常规、肝肾功能/肝胆代谢、胸部CT分别说明。"
            "如果上下文中已经有当前日期报告，不要使用“如果复查”这类假设语气；应直接基于已给出的当前报告判断升高、下降、恢复、稳定或未见异常。"
            "没有对应当前报告的项目才说明资料不足。"
        )
    parts.append("【患者基础信息】\n" + json.dumps(patient, ensure_ascii=False))
    if history:
        # 只把用户自己的近期追问作为对话连续性；旧 assistant 回答不能当医学事实，
        # 否则错误解读会被下一轮再次喂给模型。
        recent = [item for item in history[-8:] if item.get("role") == "user" and item.get("content")]
        if recent:
            parts.append("【最近对话】\n" + "\n".join([f"{m.get('role','user')}: {str(m.get('content',''))[:200]}" for m in recent]))
    parts.append("【用户当前问题】\n" + message)
    ctx = build_context(evidence)
    if ctx and ctx != "暂无可用证据。":
        parts.append(
            "【可参考的医学知识、历史报告与联网资料】\n"
            "这些资料优先级低于本轮附件，只能作为背景参考。"
            "其中 source 为 upload:: 或 postgres:: 的内容均是历史相关报告，不是本轮附件；"
            "不得把历史报告里的发现、指标异常或诊断倾向说成当前报告仍然存在。"
            "引用历史报告时必须使用“既往/历史/上次/过去报告提示”等措辞。\n"
            + ctx[:6000]
        )
    parts.append("请按系统提示的顺序，用自然中文回答。最后一行务必输出 <meta>{...}</meta>。")
    return "\n\n".join(parts)


META_PATTERN = re.compile(r"<meta>\s*(\{[\s\S]*?\})\s*</meta>", re.IGNORECASE)


def strip_meta_tag(text: str) -> tuple[str, Dict[str, Any]]:
    """从自然文本里抠出 meta JSON，并把 <meta>...</meta> 从展示文本里删掉。"""
    if not text:
        return text or "", {}
    match = META_PATTERN.search(text)
    if not match:
        return text.strip(), {}
    raw = match.group(1)
    try:
        meta = json.loads(raw)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    cleaned = (text[: match.start()] + text[match.end():]).strip()
    return cleaned, meta


def sanitize_internal_text(text: str) -> str:
    """隐藏内部 doc_id / source id，并清理模型偶尔吐出的孤立 meta 符号。"""
    if not text:
        return ""
    cleaned = re.sub(r"\s*\(?\s*doc_id\s*=\s*[0-9a-fA-F-]{8,}\s*\)?", "", text)
    cleaned = re.sub(r"\b(?:upload|postgres)::[0-9a-fA-F-]{8,}\b", "历史报告", cleaned)
    cleaned = re.sub(r"^\s*<\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def display_evidence_source(source: str) -> str:
    if str(source).startswith("upload::") or str(source).startswith("postgres::"):
        return "用户历史报告"
    if str(source) == "memory-filter":
        return "报告记忆筛选"
    return str(source or "知识库")


def public_evidence(item: Evidence) -> Evidence:
    return Evidence(
        source=display_evidence_source(item.source),
        title=sanitize_internal_text(item.title),
        score=item.score,
        content=sanitize_internal_text(item.content),
    )


def parse_json_object(text: str) -> Dict[str, Any]:
    """从模型输出里提取第一个 JSON object，路由失败时返回空 dict。"""
    if not text:
        return {}
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def attachment_router_summary(attachments: List[Dict[str, Any]]) -> str:
    """给 Router Prompt 用的短摘要，避免把完整原文塞进意图识别。"""
    lines: List[str] = []
    for idx, doc in enumerate(attachments[:6], start=1):
        parsed = doc.get("parsed_json") or {}
        title = doc.get("title") or parsed.get("title") or doc.get("file_name") or "未命名报告"
        summary = parsed.get("summary") or doc.get("summary") or ""
        findings = parsed.get("findings") or ""
        impression = parsed.get("impression") or ""
        lines.append(
            f"{idx}. {title}｜类型={doc.get('doc_type') or parsed.get('doc_type') or ''}\n"
            f"摘要={str(summary)[:300]}\n所见={str(findings)[:240]}\n结论={str(impression)[:240]}"
        )
    return "\n\n".join(lines) if lines else "无本轮附件"


SESSION_REPORT_TERMS = [
    "刚才", "刚刚", "这份", "这个报告", "这张", "最新报告", "最新的", "本次报告",
    "现在", "为什么没有", "为什么没了", "同一家医院", "上面", "前面", "它",
    "这个严重", "还要复查", "会不会", "是不是好了", "结节", "指标", "CT", "检查结果",
]


def rule_based_intent_plan(
    scene: str,
    message: str,
    attachments: List[Dict[str, Any]],
    session_reports: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """路由模型不可用时的保守兜底。"""
    session_reports = session_reports or []
    has_attachments = bool(attachments) and scene != "triage"
    has_session_reports = bool(session_reports) and scene == "consultation"
    use_session_reports = has_session_reports and any(term in message for term in SESSION_REPORT_TERMS)
    if scene == "triage":
        intent = "triage"
        use_memory = False
        use_current = False
    elif has_attachments and is_compare_question(message):
        intent = "longitudinal_compare"
        use_memory = True
        use_current = True
    elif has_attachments:
        intent = "current_report_with_history"
        use_memory = True
        use_current = True
    elif any(term in message for term in ["历史报告", "之前的报告", "上次体检", "我上传过", "以前的检查"]):
        intent = "history_lookup"
        use_memory = True
        use_current = False
    elif any(term in message for term in ["相比", "变化", "复查", "之前", "上次"]):
        intent = "longitudinal_compare"
        use_memory = True
        use_current = False
        use_session_reports = has_session_reports
    elif use_session_reports:
        intent = "report_followup"
        use_memory = False
        use_current = False
    else:
        intent = "ordinary_consultation"
        use_memory = False
        use_current = False
    profile = build_report_memory_profile(message, attachments, session_reports if use_session_reports else [])
    return {
        "intent": intent,
        "use_current_attachments": use_current,
        "use_session_reports": use_session_reports,
        "use_user_report_memory": use_memory,
        "memory_purpose": "find_related_prior_reports" if use_memory else "none",
        "retrieval_query": profile.get("query") or message,
        "evidence_policy": "current_attachment_first" if use_current else ("session_report_as_current_context" if use_session_reports else "knowledge_first"),
        "reason": "rule_based_fallback",
    }


def build_attachment_blob(attachments: List[Dict[str, Any]]) -> str:
    """把附件解析结果拼成医生友好的中文摘要文本，注入 user prompt。"""
    if not attachments:
        return ""
    blocks: List[str] = []
    for idx, doc in enumerate(attachments, start=1):
        parsed = doc.get("parsed_json") or {}
        summary = build_report_summary(parsed) if parsed else (doc.get("summary") or "")
        title = doc.get("title") or parsed.get("title") or "未命名报告"
        confidence = doc.get("confidence") or parsed.get("confidence") or 0.0
        raw_text = parsed.get("raw_text") or parsed.get("raw") or ""
        findings = parsed.get("findings") or ""
        impression = parsed.get("impression") or ""
        block = f"[本轮附件 {idx}] {title}（解析置信度 {confidence}）\n{summary[:1500]}"
        if findings or impression:
            block += f"\n【本轮报告结构化所见/结论】\n{findings}\n{impression}".strip()
        if raw_text:
            block += "\n【本轮报告原文节选】\n" + str(raw_text)[:1800]
        blocks.append(block)
    return "\n\n".join(blocks)


def build_session_report_blob(reports: List[Dict[str, Any]]) -> str:
    """把本会话历史上传报告压缩成可追问上下文；第一份视为最近报告。"""
    if not reports:
        return ""
    blocks: List[str] = []
    for idx, doc in enumerate(reports[:5], start=1):
        parsed = doc.get("parsed_json") or {}
        title = doc.get("title") or parsed.get("title") or doc.get("file_name") or "未命名报告"
        summary = build_report_summary(parsed) if parsed else (doc.get("summary") or "")
        raw_text = parsed.get("raw_text") or ""
        report_date = parsed.get("report_date") or doc.get("created_at") or datetime.now().isoformat(timespec="seconds")
        label = "最近一次已知报告" if idx == 1 else "较早历史报告"
        block = (
            f"[{label} {idx}] {title}｜报告日期/时间={report_date}\n"
            f"{summary[:1300]}"
        )
        if raw_text:
            block += "\n【报告原文节选】\n" + str(raw_text)[:1200]
        blocks.append(block)
    return "\n\n".join(blocks)


def build_context(evidence: List[Evidence]) -> str:
    if not evidence:
        return "暂无可用证据。"
    return "\n\n".join(
        [
            f"[{idx + 1}] {sanitize_internal_text(item.title)} | {display_evidence_source(item.source)} | score={item.score}\n{sanitize_internal_text(item.content)[:1500]}"
            for idx, item in enumerate(evidence[:12])
        ]
    )


COMPARE_TERMS = ["相比", "变化", "对比", "复查", "三个月前", "之前", "较前", "趋势"]
PEDIATRIC_TERMS = ["小孩", "孩子", "儿童", "宝宝", "婴儿", "幼儿", "小儿", "儿科", "上小学", "上幼儿园"]
MONTH_WORDS = {
    "一": "01", "二": "02", "三": "03", "四": "04", "五": "05", "六": "06",
    "七": "07", "八": "08", "九": "09", "十": "10", "十一": "11", "十二": "12",
}
REPORT_TOPIC_TERMS = {
    "chest_ct": ["胸部", "肺部", "肺", "ct", "CT", "结节", "磨玻璃", "条索", "纤维灶", "影像"],
    "blood": ["血常规", "血红蛋白", "白细胞", "红细胞", "血小板", "中性粒", "淋巴细胞", "贫血"],
    "liver_kidney": ["肝", "肾", "胆红素", "胆汁酸", "转氨酶", "肌酐", "尿素", "尿酸", "肝肾功能", "生化"],
}
CLINICAL_SYSTEM_TERMS = {
    "呼吸系统": ["胸部", "肺部", "肺", "咳嗽", "咳痰", "气短", "胸闷", "胸痛", "CT", "结节", "磨玻璃", "条索影", "纤维灶"],
    "肝胆胰脾": ["腹部", "肝", "胆", "胰", "脾", "彩超", "超声", "胆红素", "胆汁酸", "转氨酶", "脂肪肝", "胆囊"],
    "肾脏泌尿": ["肾", "输尿管", "膀胱", "尿", "肌酐", "尿素", "尿酸", "蛋白尿", "泌尿"],
    "血液免疫": ["血常规", "白细胞", "红细胞", "血红蛋白", "血小板", "中性粒", "淋巴细胞", "贫血", "感染"],
    "消化系统": ["胃", "肠", "腹痛", "腹泻", "恶心", "呕吐", "便秘", "消化", "胃镜", "肠镜"],
    "内分泌代谢": ["血糖", "糖化", "甲状腺", "血脂", "尿酸", "代谢", "胰岛素"],
}
REPORT_MEMORY_INTENT_TERMS = [
    "报告", "检查", "检验", "化验", "体检", "CT", "彩超", "B超", "核磁", "指标",
    "之前", "历史", "上次", "复查", "相比", "变化", "结合我", "我上传过",
]


def is_compare_question(text: str) -> bool:
    return any(word in text for word in COMPARE_TERMS)


def mentions_child(text: str, patient: Dict[str, Any] | None = None) -> bool:
    if any(term in text for term in PEDIATRIC_TERMS):
        return True
    try:
        age = int((patient or {}).get("age") or 0)
        return 0 < age < 14
    except Exception:
        return False


def extract_month_hints(text: str) -> set[str]:
    months: set[str] = set()
    for raw in re.findall(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", text):
        months.add(raw.zfill(2))
    for raw in re.findall(r"20\d{2}[-/年](1[0-2]|0?[1-9])", text):
        months.add(raw.zfill(2))
    for cn, mm in MONTH_WORDS.items():
        if f"{cn}月" in text:
            months.add(mm)
    return months


def infer_report_topics(text: str) -> set[str]:
    topics: set[str] = set()
    for topic, terms in REPORT_TOPIC_TERMS.items():
        if any(term in text for term in terms):
            topics.add(topic)
    return topics


def infer_clinical_systems(text: str) -> set[str]:
    systems: set[str] = set()
    for system, terms in CLINICAL_SYSTEM_TERMS.items():
        if any(term in text for term in terms):
            systems.add(system)
    return systems


def build_report_memory_profile(
    message: str,
    attachments: List[Dict[str, Any]],
    session_reports: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """把当前问题和本轮报告解析结果压缩成检索画像，供历史报告 RAG 使用。"""
    attachment_parts: List[str] = []
    report_types: set[str] = set()
    titles: set[str] = set()
    for doc in [*(attachments or []), *((session_reports or []))]:
        parsed = doc.get("parsed_json") or {}
        report_types.add(str(doc.get("doc_type") or parsed.get("doc_type") or ""))
        titles.add(str(doc.get("title") or parsed.get("title") or doc.get("file_name") or ""))
        attachment_parts.extend(
            [
                str(parsed.get("summary") or doc.get("summary") or ""),
                str(parsed.get("findings") or ""),
                str(parsed.get("impression") or ""),
                str(parsed.get("raw_text") or ""),
            ]
        )
    context_text = "\n".join([message, *attachment_parts])
    systems = infer_clinical_systems(context_text)
    topics = infer_report_topics(context_text)
    key_terms: List[str] = []
    for system in systems:
        key_terms.extend(CLINICAL_SYSTEM_TERMS[system][:8])
    for topic in topics:
        key_terms.extend(REPORT_TOPIC_TERMS[topic][:8])
    key_terms.extend([item for item in titles if item])
    return {
        "systems": sorted(systems),
        "topics": sorted(topics),
        "report_types": sorted(item for item in report_types if item),
        "titles": sorted(item for item in titles if item),
        "query": " ".join(dict.fromkeys([message, *key_terms])),
        "has_attachment": bool(attachments),
        "needs_memory": bool(attachments) or any(term in message for term in REPORT_MEMORY_INTENT_TERMS),
    }


def evidence_profile_score(item: Evidence, profile: Dict[str, Any]) -> float:
    """历史报告相关性门控：向量相似度只是底座，医学系统重合决定是否进入上下文。"""
    text = f"{item.title}\n{item.content}"
    score = float(item.score or 0.0) * 0.55
    systems = set(profile.get("systems") or [])
    topics = set(profile.get("topics") or [])
    matched_systems = systems & infer_clinical_systems(text)
    if systems:
        score += 0.35 * (len(matched_systems) / len(systems))
    if topics:
        score += 0.20 * evidence_topic_score(item, topics)
    for report_type in profile.get("report_types") or []:
        if report_type and report_type in text:
            score += 0.08
    if "优先级=high" in text or "片段=summary" in text or "片段=postgres_full" in text:
        score += 0.04
    return round(score, 4)


def evidence_matches_month(item: Evidence, months: set[str]) -> bool:
    if not months:
        return True
    text = f"{item.title}\n{item.content}"
    return any(
        token in text
        for month in months
        for token in [f"-{month}-", f"-{month}T", f"年{int(month)}月", f"报告日期=2026-{month}", f"上传时间=2026-{month}"]
    )


def evidence_topic_score(item: Evidence, topics: set[str]) -> float:
    if not topics:
        return 0.0
    text = f"{item.title}\n{item.content}"
    matched = 0
    for topic in topics:
        if any(term in text for term in REPORT_TOPIC_TERMS[topic]):
            matched += 1
    return matched / max(len(topics), 1)


def expand_report_queries(query: str, message: str, attachment_blob: str = "") -> List[str]:
    """多路召回：基础语义、检查类型、时间线各查一次，再合并去重。"""
    topics = infer_report_topics(message + "\n" + attachment_blob)
    months = extract_month_hints(message)
    queries = [query]
    if attachment_blob:
        queries.append((message + "\n" + attachment_blob[:1800]).strip())
    for topic in topics:
        queries.append(f"{message} {' '.join(REPORT_TOPIC_TERMS[topic])}")
    if months:
        queries.append(f"{message} 报告日期 上传时间 {' '.join(sorted(months))} 月份")
    if is_compare_question(message):
        queries.append(f"{message} 历史报告 复查 对比 变化 趋势")
    deduped: List[str] = []
    for item in queries:
        item = item.strip()
        if item and item not in deduped:
            deduped.append(item)
    return deduped[:5]


def keyword_report_candidates(user_id: str, message: str, limit: int = 20, exclude_doc_ids: set[str] | None = None) -> List[Evidence]:
    """从 PostgreSQL 元数据补召回报告，解决纯向量检索漏掉日期/检查名的问题。"""
    exclude_doc_ids = exclude_doc_ids or set()
    months = extract_month_hints(message)
    topics = infer_report_topics(message)
    compare_mode = is_compare_question(message)
    rows = list_medical_documents(user_id, limit=limit)
    candidates: List[tuple[float, Evidence]] = []
    for row in rows:
        if row["doc_id"] in exclude_doc_ids:
            continue
        detail = get_medical_document(row["doc_id"])
        if not detail:
            continue
        parsed = detail.get("parsed_json") or {}
        meta_text = "\n".join(
            [
                str(detail.get("title") or ""),
                str(detail.get("doc_type") or ""),
                str(detail.get("created_at") or ""),
                str(parsed.get("report_date") or ""),
                str(parsed.get("summary") or detail.get("summary") or ""),
                str(parsed.get("findings") or ""),
                str(parsed.get("impression") or ""),
                str(parsed.get("raw_text") or ""),
            ]
        )
        evidence = Evidence(
            source=f"postgres::{detail['doc_id']}",
            title="｜".join(
                part
                for part in [
                    str(detail.get("title") or "医疗报告"),
                    str(parsed.get("report_date") or detail.get("created_at") or ""),
                    str(detail.get("doc_type") or ""),
                ]
                if part
            ),
            score=0.72,
            content=(
                "【报告元数据】\n"
                f"doc_id={detail['doc_id']}\n"
                f"标题={detail.get('title') or ''}\n"
                f"报告类型={detail.get('doc_type') or ''}\n"
                f"报告日期={parsed.get('report_date') or '未知'}\n"
                f"上传时间={detail.get('created_at') or '未知'}\n"
                f"片段=postgres_full\n\n"
                f"【摘要】\n{parsed.get('summary') or detail.get('summary') or ''}\n\n"
                f"【影像/检验原文】\n{(parsed.get('raw_text') or parsed.get('findings') or parsed.get('impression') or '')[:1800]}"
            ),
        )
        score = 0.72
        if topics:
            score += evidence_topic_score(evidence, topics) * 0.45
            if evidence_topic_score(evidence, topics) <= 0:
                score -= 0.25
        if months:
            if evidence_matches_month(evidence, months):
                score += 0.45
            elif not compare_mode:
                continue
        if not topics and not months and not compare_mode:
            continue
        if any(term in meta_text for term in ["结节", "血常规", "胆红素", "肌酐", "胸部", "肺部", "CT"]):
            score += 0.05
        candidates.append((score, Evidence(source=evidence.source, title=evidence.title, score=round(score, 4), content=evidence.content)))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in candidates[:10]]


def evidence_doc_id(item: Evidence) -> str:
    text = f"{item.source}\n{item.content}"
    match = re.search(r"(?:upload::|postgres::|doc_id=)([0-9a-fA-F-]{8,})", text)
    return match.group(1) if match else ""


def text_negates_lung_nodule(text: str) -> bool:
    """识别“本轮肺部报告未提示结节/占位”的常见表达。"""
    if not text:
        return False
    negative_terms = ["未见", "无", "未发现", "未提示", "未见明确", "未见明显"]
    target_terms = ["结节", "占位", "磨玻璃影", "磨玻璃结节", "实变影", "占位性病变"]
    for neg in negative_terms:
        for target in target_terms:
            pattern = rf"{neg}[^。\n；;，,]{{0,18}}{target}|{target}[^。\n；;，,]{{0,18}}{neg}"
            if re.search(pattern, text):
                return True
    return any(term in text for term in ["未见明确肺部占位", "未见明显占位", "未见肺结节", "未见明确结节", "未见明确占位性病变", "未见明显占位性病变"])


def filter_report_memory(message: str, reports: List[Evidence], attachments: List[Dict[str, Any]] | None = None) -> tuple[List[Evidence], List[str]]:
    """对用户报告做二次重排：由当前问题和报告解析画像驱动，低相关旧报告不进上下文。"""
    if not reports:
        return [], []
    attachments = attachments or []
    current_doc_ids = {str(doc.get("doc_id")) for doc in attachments if doc.get("doc_id")}
    profile = build_report_memory_profile(message, attachments)
    compare_mode = is_compare_question(message)
    target_months = extract_month_hints(message)
    target_topics = infer_report_topics(message)
    current_attachment_text = "\n".join(
        [
            str((doc.get("parsed_json") or {}).get("summary") or "")
            + "\n"
            + str((doc.get("parsed_json") or {}).get("findings") or "")
            + "\n"
            + str((doc.get("parsed_json") or {}).get("impression") or "")
            + "\n"
            + str((doc.get("parsed_json") or {}).get("raw_text") or "")
            for doc in attachments
        ]
    )
    has_current_report = bool(current_attachment_text.strip())
    current_says_no_nodule = text_negates_lung_nodule(current_attachment_text)
    current_mentions_old_fibrosis = any(term in current_attachment_text for term in ["纤维灶", "陈旧性改变", "条索影"])
    current = message.lower()
    heart_terms = ["胸痛", "胸闷", "心慌", "心悸", "心脏", "气短"]
    infection_terms = ["发热", "咳嗽", "感冒", "感染", "白细胞", "中性粒", "crp", "炎症"]
    notes: List[str] = []
    ranked: List[tuple[float, Evidence]] = []
    for index, item in enumerate(reports):
        item_doc_id = evidence_doc_id(item)
        if item_doc_id and item_doc_id in current_doc_ids:
            notes.append(f"排除本轮附件《{item.title}》的重复召回：当前附件只作为最高优先级证据，不进入历史报告记忆。")
            continue
        content = (item.title + "\n" + item.content).lower()
        score = evidence_profile_score(item, profile)
        score += evidence_topic_score(item, target_topics) * 0.12
        if target_months:
            if evidence_matches_month(item, target_months):
                score += 0.12
            elif not compare_mode:
                score -= 0.08
        if "优先级=high" in content or "片段=summary" in content:
            score += 0.08
        score -= index * 0.01
        if has_current_report and str(item.source).startswith("upload::"):
            if current_says_no_nodule and any(term in content for term in ["结节", "磨玻璃结节", "实性结节"]):
                notes.append(f"对比历史报告《{item.title}》：既往提示结节，但本轮报告未提示明确结节，应表述为较前消失/未再显示，而不是当前仍有结节。")
                ranked.append((score + 0.2, Evidence(source=item.source, title=f"历史相关报告（非本轮附件）：{item.title}", score=item.score, content=item.content)))
                continue
            if current_mentions_old_fibrosis and "结节" in content and "纤维灶" not in content:
                notes.append(f"对比历史报告《{item.title}》：本轮更偏纤维灶/陈旧改变，旧结节描述只能作为既往背景和变化线索。")
                ranked.append((score + 0.15, Evidence(source=item.source, title=f"历史相关报告（非本轮附件）：{item.title}", score=item.score, content=item.content)))
                continue
        is_infection_report = any(t.lower() in content for t in infection_terms)
        current_is_heart = any(t in current for t in heart_terms)
        current_is_infection = any(t.lower() in current for t in infection_terms)
        if current_is_heart and is_infection_report and not current_is_infection:
            notes.append(f"忽略历史报告《{item.title}》：更像感染/血常规背景，与当前心脏不适关联弱。")
            continue
        ranked.append((score, Evidence(source=item.source, title=f"历史相关报告（非本轮附件）：{item.title}", score=round(score, 4), content=item.content)))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    threshold = 0.42 if attachments else 0.36
    kept = [item for score, item in ranked if score >= threshold]
    if not kept and ranked and profile.get("needs_memory"):
        # 没有强相关证据时最多给 1 条候选，并明确标注弱相关，避免模型误用。
        weak_score, weak_item = ranked[0]
        if weak_score >= 0.30:
            kept = [weak_item]
            notes.append("仅找到一条弱相关历史报告，回答时必须说明相关性有限，不能据此推断当前报告结论。")
    limit = 10 if compare_mode else 6
    if not kept:
        notes.append("未找到与当前问题/本轮报告高度相关的历史报告，本次不使用旧报告记忆。")
    return kept[:limit], notes


def build_search_query(scene: str, message: str, patient: Dict[str, Any], history: List[Dict[str, Any]]) -> str:
    history_text = " ".join([str(item.get("content", "")) for item in history[-6:] if item.get("role") == "user"])
    if scene == "medication":
        return f"用药安全 药物相互作用 禁忌 特殊人群 {message} {patient}"
    if scene == "triage":
        return f"智能分诊 科室定位 推荐逻辑 科室别名 红旗症状 主诉症状 推荐科室 {message} {patient}"
    return f"线上问诊 健康科普 科室定位 推荐科室 就医建议 {message} {history_text} {patient}"


def normalize_department(value: Any, scene: str) -> str:
    text = str(value or "").strip()
    if text in DEPARTMENTS:
        return text
    if scene == "medication":
        return "药学门诊"
    for standard, aliases in DEPARTMENT_ALIASES.items():
        if text == standard or any(alias and alias in text for alias in aliases):
            return standard
    for department in DEPARTMENTS:
        if department in text:
            return department
    return "普通内科" if "普通内科" in DEPARTMENTS else DEPARTMENTS[0]


def normalize_risk(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"低风险", "中风险", "高风险"}:
        return text
    if "高" in text:
        return "高风险"
    if "中" in text:
        return "中风险"
    return "低风险"


def build_local_fallback_answer(message: str, risk_hint: Dict[str, Any], department: str) -> str:
    """远程模型完全不可用时的最小安全回答，保证用户仍拿到基本就医边界。"""
    risk = risk_hint.get("risk_level") or "低风险"
    red_flags = risk_hint.get("red_flags") or []
    lines = [
        "远程模型暂时连接不稳定，我先给你一个基础的安全建议。",
        f"你描述的是：{message[:120]}",
        f"从本地规则看，当前先按 **{risk}** 处理；建议优先咨询或挂号 **{department}**。",
    ]
    if red_flags:
        lines.append("需要特别注意：" + "、".join(red_flags) + "。如果这些情况明显或持续加重，建议及时线下就医或急诊。")
    else:
        lines.append("如果症状持续加重、出现胸痛气短、意识改变、持续高热、大量呕吐/腹泻或脱水表现，请及时线下就医。")
    lines.append("你也可以稍后重试，我会结合 RAG、联网资料和历史报告给出更完整的分析。")
    return "\n\n".join(lines)


# =========================================================
# Swarm 流式执行核心
# =========================================================

class MedicalSwarm:
    """流式 Swarm：RAG（Chroma 双 collection）+ DeepResearch + 自然语言 LLM。"""

    def __init__(self):
        self.legacy_rag = RAGService()
        self.chroma = get_chroma_service()
        self.use_chroma = bool(SETTINGS.get("rag", {}).get("use_chroma", True)) and self.chroma.available
        self.llm = LLMClient()

    async def build_intent_plan(
        self,
        scene: str,
        message: str,
        patient: Dict[str, Any],
        history: List[Dict[str, Any]],
        attachments: List[Dict[str, Any]],
        session_reports: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """用轻量 Router Prompt 判断本轮任务和报告记忆检索策略。"""
        session_reports = session_reports or []
        fallback = rule_based_intent_plan(scene, message, attachments, session_reports)
        if scene == "triage" or not self.llm.enabled:
            return fallback
        if not SETTINGS.get("features", {}).get("enable_llm_router", False):
            return fallback
        recent_user = [
            str(item.get("content", ""))[:160]
            for item in history[-6:]
            if item.get("role") == "user" and item.get("content")
        ]
        system = (
            "你是医疗问诊系统的任务路由器。只判断任务意图和检索策略，不回答医学问题。"
            "你必须只输出一个 JSON object，不要输出 Markdown。"
        )
        user = f"""
可选 intent：
- ordinary_consultation：普通健康问答，不需要用户历史报告。
- current_report_interpretation：主要解读本轮上传报告。
- current_report_with_history：本轮报告需要结合语义相关的历史报告作背景分析。
- report_followup：没有新附件，但用户在追问本会话刚才/最新/前面上传过的报告。
- longitudinal_compare：用户明确或隐含想比较前后变化。
- history_lookup：用户查询过去某份报告或历史检查。
- symptom_with_memory：用户描述症状，历史检查可能提供重要背景。
- triage：智能分诊。

请判断：
1. 是否使用本轮附件。
2. 是否检索用户历史报告。
3. 如果需要检索，生成面向历史报告向量库的 retrieval_query。
4. 给出 evidence_policy，说明最终回答如何使用当前附件和历史报告。

场景：{scene}
患者基础信息：{json.dumps(patient, ensure_ascii=False)}
最近用户消息：{json.dumps(recent_user, ensure_ascii=False)}
本轮附件摘要：
{attachment_router_summary(attachments)}
本会话已上传报告摘要：
{attachment_router_summary(session_reports)}
用户当前问题：
{message}

输出 JSON 字段：
{{
  "intent": "上面八种之一",
  "use_current_attachments": true/false,
  "use_session_reports": true/false,
  "use_user_report_memory": true/false,
  "memory_purpose": "none/find_related_prior_reports/compare_trend/look_up_specific_report",
  "retrieval_query": "中文医学检索 query；不需要检索时为空字符串",
  "evidence_policy": "current_attachment_first/history_background_only/history_compare/current_only/knowledge_first",
  "reason": "一句话说明"
}}
"""
        try:
            raw = await self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                timeout=18.0,
            )
            data = parse_json_object(raw)
        except Exception as exc:
            log_step("router.intent.failed", scene=scene, error=type(exc).__name__)
            return fallback
        allowed = {
            "ordinary_consultation",
            "current_report_interpretation",
            "current_report_with_history",
            "report_followup",
            "longitudinal_compare",
            "history_lookup",
            "symptom_with_memory",
            "triage",
        }
        intent = str(data.get("intent") or fallback["intent"])
        if intent not in allowed:
            intent = fallback["intent"]
        use_current = bool(data.get("use_current_attachments")) and bool(attachments) and scene != "triage"
        use_session = bool(data.get("use_session_reports")) and bool(session_reports) and scene == "consultation"
        use_memory = bool(data.get("use_user_report_memory")) and scene == "consultation"
        if attachments and scene == "consultation" and intent in {"current_report_with_history", "longitudinal_compare"}:
            use_memory = True
        if use_session and intent == "ordinary_consultation":
            intent = "report_followup"
        profile = build_report_memory_profile(message, attachments, session_reports if use_session else [])
        query = str(data.get("retrieval_query") or "").strip() or str(profile.get("query") or message)
        return {
            "intent": intent,
            "use_current_attachments": use_current,
            "use_session_reports": use_session,
            "use_user_report_memory": use_memory,
            "memory_purpose": str(data.get("memory_purpose") or fallback["memory_purpose"]),
            "retrieval_query": query,
            "evidence_policy": str(data.get("evidence_policy") or fallback["evidence_policy"]),
            "reason": str(data.get("reason") or "router_prompt"),
        }

    async def run_stream(
        self,
        scene: str,
        message: str,
        patient: Dict[str, Any],
        history: List[Dict[str, Any]] | None = None,
        user_id: str = "demo_user",
        attachments: List[Dict[str, Any]] | None = None,
        session_reports: List[Dict[str, Any]] | None = None,
    ):
        """按 SSE 事件 yield dict：trace / evidence / chunk / final。"""
        history = history or []
        attachments = attachments or []
        session_reports = session_reports or []
        trace_list: List[AgentTrace] = []

        def emit_trace(agent: str, action: str, detail: str) -> Dict[str, Any]:
            t = AgentTrace(agent=agent, action=action, detail=detail)
            trace_list.append(t)
            log_step("agent.trace", user_id=user_id, scene=scene, agent=agent, action=action, detail=detail)
            return {"type": "trace", "agent": agent, "action": action, "detail": detail}

        yield emit_trace("RouterAgent", "route", f"识别业务场景为 {SCENE_NAMES.get(scene, scene)}，进入综合推理链路")

        if scene != "triage" and attachments:
            yield emit_trace(
                "AttachmentAgent",
                "ingest",
                f"用户随消息附带 {len(attachments)} 份报告/影像：" + "、".join([str(a.get("title") or a.get("file_name") or "") for a in attachments]),
            )
        elif scene == "triage" and attachments:
            yield emit_trace("AttachmentAgent", "skip", "智能分诊不使用附件报告，本轮仅依据症状描述和患者基础信息分诊")

        risk_hint = assess_risk(message)
        suggestions = lifestyle_recommendations(message)
        yield emit_trace(
            "ContextAgent",
            "profile",
            f"整理患者上下文与历史对话；本地风险提示：{risk_hint['risk_level']}（仅作参考）",
        )

        intent_plan = await self.build_intent_plan(scene, message, patient, history, attachments, session_reports)
        yield emit_trace(
            "RouterAgent",
            "intent",
            f"任务意图：{intent_plan.get('intent')}；历史报告检索：{'开启' if intent_plan.get('use_user_report_memory') else '关闭'}；策略：{intent_plan.get('evidence_policy')}",
        )

        query = build_search_query(scene, message, patient, history)
        if self.use_chroma:
            kb_task = self.chroma.query_kb(query)
            web_task = web_search(query, limit=3, timeout=6.0)
            report_evidence: List[Evidence] = []
            filtered_reports: List[Evidence] = []
            filter_notes: List[str] = []
            if scene == "consultation":
                active_session_reports = session_reports if intent_plan.get("use_session_reports") else []
                memory_profile = build_report_memory_profile(message, attachments, active_session_reports)
                if intent_plan.get("use_user_report_memory"):
                    profile_query = str(intent_plan.get("retrieval_query") or memory_profile.get("query") or query)
                    attachment_search_blob = build_attachment_blob(attachments) + "\n" + build_session_report_blob(active_session_reports)
                    report_queries = expand_report_queries(profile_query, message, attachment_search_blob)
                    report_tasks = [
                        self.chroma.query_user_reports(item, user_id=user_id, top_k=14)
                        for item in report_queries
                    ]
                    gathered = await asyncio.gather(kb_task, *report_tasks, web_task)
                    kb_evidence = gathered[0]
                    report_groups = gathered[1:-1]
                    web_evidence = gathered[-1]
                    report_map: Dict[str, Evidence] = {}
                    for group in report_groups:
                        for item in group:
                            key = f"{item.source}|{item.title}|{item.content[:80]}"
                            old = report_map.get(key)
                            if not old or item.score > old.score:
                                report_map[key] = item
                    current_doc_ids = {
                        str(doc.get("doc_id"))
                        for doc in [*attachments, *active_session_reports]
                        if doc.get("doc_id")
                    }
                    for item in keyword_report_candidates(user_id, profile_query, exclude_doc_ids=current_doc_ids):
                        key = f"{item.source}|{item.title}"
                        old = report_map.get(key)
                        if not old or item.score > old.score:
                            report_map[key] = item
                    report_evidence = sorted(report_map.values(), key=lambda item: item.score, reverse=True)
                    filtered_reports, filter_notes = filter_report_memory(message, list(report_evidence), [*attachments, *active_session_reports])
                    yield emit_trace(
                        "MemoryRAGAgent",
                        "semantic_retrieve",
                        f"按当前报告/问题画像召回历史报告 {len(report_evidence)} 条，相关性门控后采用 {len(filtered_reports)} 条",
                    )
                else:
                    kb_evidence, web_evidence = await asyncio.gather(kb_task, web_task)
                    yield emit_trace("MemoryRAGAgent", "skip", "当前问题未触发报告记忆检索")
            else:
                kb_evidence, web_evidence = await asyncio.gather(kb_task, web_task)
                yield emit_trace("MemoryRAGAgent", "skip", "智能分诊不检索用户历史报告")
            evidence = list(kb_evidence) + filtered_reports + list(web_evidence)
            yield emit_trace("RAGAgent", "retrieve", f"Chroma 知识库召回 {len(kb_evidence)} 条")
            if filter_notes:
                evidence.append(Evidence(source="memory-filter", title="历史报告相关性说明", score=1.0, content="\n".join(filter_notes)))
            yield emit_trace("ResearchAgent", "deep_search", f"联网搜索返回 {len(web_evidence)} 条证据")
        else:
            local_task = asyncio.to_thread(self.legacy_rag.search, query, 6)
            web_task = web_search(query, limit=3, timeout=6.0)
            local_evidence, web_evidence = await asyncio.gather(local_task, web_task)
            evidence = list(local_evidence) + list(web_evidence)
            yield emit_trace("RAGAgent", "retrieve", f"本地知识库召回 {len(local_evidence)} 条证据（词袋）")
            yield emit_trace("ResearchAgent", "deep_search", f"联网搜索返回 {len(web_evidence)} 条证据")

        yield {"type": "evidence", "items": [public_evidence(e).model_dump() for e in evidence[:8]]}

        session_report_blob = (
            build_session_report_blob(session_reports)
            if scene != "triage" and intent_plan.get("use_session_reports")
            else ""
        )
        attachment_blob = "" if scene == "triage" else build_attachment_blob(attachments)
        system_prompt = build_natural_system_prompt(scene)
        user_prompt = build_natural_user_prompt(
            scene,
            message,
            patient,
            history,
            evidence,
            attachment_blob,
            session_report_blob,
            intent_plan,
        )

        yield emit_trace("ReasoningAgent", "stream_start", "开始流式生成自然语言回答")

        accumulated: List[str] = []
        in_meta = False
        try:
            async for delta in self.llm.chat_stream(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                timeout=120.0,
                max_tokens=1500,
                temperature=float(SETTINGS.get("llm", {}).get("temperature", 0.3)),
            ):
                accumulated.append(delta)
                full = "".join(accumulated)
                # 进入 <meta> 段后不再向前端推送（meta 给系统用、不给用户看）
                if not in_meta and "<meta>" in full:
                    in_meta = True
                    head = full.split("<meta>", 1)[0]
                    already = "".join(accumulated[:-1])
                    pending = head[len(already):] if len(head) >= len(already) else ""
                    if pending:
                        yield {"type": "chunk", "delta": pending}
                    continue
                if in_meta:
                    continue
                yield {"type": "chunk", "delta": delta}
        except Exception as exc:
            yield emit_trace("ReasoningAgent", "llm_error", f"远程模型流式失败：{type(exc).__name__}")
            department = normalize_department(risk_hint.get("department"), scene)
            err_text = build_local_fallback_answer(message, risk_hint, department)
            accumulated = [err_text]
            yield {"type": "chunk", "delta": err_text}

        full_text = "".join(accumulated)
        answer_text, meta = strip_meta_tag(full_text)
        if not answer_text.strip():
            answer_text = "（模型未返回有效回答，请重试或换个问法。）"

        risk_level = normalize_risk(meta.get("risk_level"))
        department = normalize_department(meta.get("recommended_department"), scene)
        if scene == "triage" and mentions_child(message, patient):
            department = "儿科"

        answer_text = sanitize_internal_text(compliance_guard(answer_text))
        yield emit_trace("SafetyAgent", "guard", "完成医疗安全边界与免责声明检查")

        metrics = {
            "agent_count": len({t.agent for t in trace_list}),
            "evidence_count": len(evidence),
            "ai_streamed": True,
        }
        yield {
            "type": "final",
            "answer": answer_text,
            "risk_level": risk_level,
            "recommended_department": department,
            "suggestions": suggestions,
            "thinking_steps": [f"{t.agent}: {t.detail}" for t in trace_list],
            "evidence": [public_evidence(e).model_dump() for e in evidence[:8]],
            "agent_trace": [t.model_dump() for t in trace_list],
            "metrics": metrics,
        }


class ConsultationService:
    def __init__(self):
        self.swarm = MedicalSwarm()

    async def chat_stream(self, req: ChatRequest, scene: str = "consultation"):
        """流式 chat：yield dict 事件，前端按 SSE 协议序列化。"""
        session_id = req.session_id or str(uuid.uuid4())
        log_step("chat.begin", user_id=req.user_id, scene=scene, session_id=session_id)
        upsert_session(session_id, req.message[:30] or "线上问诊", user_id=req.user_id, scene=scene)

        attachments_docs = (
            get_documents_by_ids(req.attached_doc_ids, user_id=req.user_id)
            if req.attached_doc_ids and scene != "triage"
            else []
        )

        msg_meta = {"scene": scene, "patient_context": req.patient_context.model_dump()}
        if attachments_docs:
            msg_meta["attached_doc_ids"] = [d["doc_id"] for d in attachments_docs]
        user_message_id = add_message(session_id, "user", req.message, msg_meta)
        if attachments_docs:
            attach_documents_to_message(
                message_id=user_message_id,
                session_id=session_id,
                user_id=req.user_id,
                doc_ids=[d["doc_id"] for d in attachments_docs],
            )

        yield {"type": "session", "session_id": session_id, "user_message_id": user_message_id}

        history = list_messages(session_id, limit=12)
        current_doc_ids = [d["doc_id"] for d in attachments_docs]
        session_reports = (
            list_session_medical_documents(
                session_id=session_id,
                user_id=req.user_id,
                limit=8,
                exclude_doc_ids=current_doc_ids if attachments_docs else [],
            )
            if scene == "consultation"
            else []
        )

        final_payload: Dict[str, Any] = {}
        async for event in self.swarm.run_stream(
            scene,
            req.message,
            req.patient_context.model_dump(),
            history,
            user_id=req.user_id,
            attachments=attachments_docs,
            session_reports=session_reports,
        ):
            if event.get("type") == "final":
                final_payload = event
                continue
            yield event

        if not final_payload:
            yield {"type": "error", "detail": "model did not return final payload"}
            return

        add_message(
            session_id,
            "assistant",
            final_payload["answer"],
            {
                "scene": scene,
                "risk_level": final_payload["risk_level"],
                "department": final_payload["recommended_department"],
                "trace": final_payload.get("agent_trace", []),
            },
        )
        add_encounter(
            session_id=session_id,
            user_id=req.user_id,
            scene=scene,
            chief_complaint=req.message,
            risk_level=final_payload["risk_level"],
            department=final_payload["recommended_department"],
            summary=final_payload["answer"],
            metadata={"evidence_count": len(final_payload.get("evidence", [])), "streamed": True},
        )
        log_step("chat.done", user_id=req.user_id, scene=scene, session_id=session_id, risk=final_payload["risk_level"])

        yield {
            "type": "done",
            "session_id": session_id,
            "risk_level": final_payload["risk_level"],
            "recommended_department": final_payload["recommended_department"],
            "suggestions": final_payload.get("suggestions", []),
            "thinking_steps": final_payload.get("thinking_steps", []),
            "evidence": final_payload.get("evidence", []),
            "agent_trace": final_payload.get("agent_trace", []),
            "metrics": final_payload.get("metrics", {}),
            "disclaimer": DISCLAIMER,
        }


# =========================================================
# 报告查询模块（mock + AI 解读）
# =========================================================

REPORTS = [
    {
        "id": "LAB2026050301",
        "type": "检验",
        "title": "血常规",
        "name": "血常规",
        "report_date": "2026-05-02",
        "date": "2026-05-02",
        "status": "部分异常",
        "items": [
            {"name": "白细胞", "value": "11.2", "unit": "10^9/L", "reference": "3.5-9.5", "range": "3.5-9.5", "flag": "偏高"},
            {"name": "中性粒细胞比例", "value": "78", "unit": "%", "reference": "40-75", "range": "40-75", "flag": "偏高"},
            {"name": "血红蛋白", "value": "136", "unit": "g/L", "reference": "130-175", "range": "130-175", "flag": "正常"},
        ],
    },
    {
        "id": "IMG2026050108",
        "type": "检查",
        "title": "胸部 CT",
        "name": "胸部 CT",
        "report_date": "2026-05-01",
        "date": "2026-05-01",
        "status": "已出报告",
        "items": [
            {"name": "影像所见", "value": "双肺纹理稍增多，未见明显实变影", "unit": "", "reference": "", "range": "", "flag": "提示随访"},
            {"name": "报告建议", "value": "结合临床症状，必要时呼吸科复诊", "unit": "", "reference": "", "range": "", "flag": "建议"},
        ],
    },
    {
        "id": "LAB2026042812",
        "type": "检验",
        "title": "肝肾功能",
        "name": "肝肾功能",
        "report_date": "2026-04-28",
        "date": "2026-04-28",
        "status": "正常",
        "items": [
            {"name": "ALT", "value": "24", "unit": "U/L", "reference": "0-40", "range": "0-40", "flag": "正常"},
            {"name": "肌酐", "value": "68", "unit": "umol/L", "reference": "45-84", "range": "45-84", "flag": "正常"},
        ],
    },
]


def report_list() -> List[Dict[str, Any]]:
    return REPORTS


async def interpret_report(report_id: str) -> Dict[str, Any]:
    report = next((item for item in REPORTS if item["id"] == report_id), None)
    if not report:
        return {"id": report_id, "analysis": "未查询到该报告。"}

    abnormal = [item for item in report["items"] if item["flag"] != "正常"]
    department = "呼吸科" if "胸" in report["name"] or "白细胞" in str(report["items"]) else "内科"
    risk_level = "中风险" if abnormal else "低风险"
    llm = LLMClient()
    if llm.enabled:
        prompt = (
            "请用患者友好的中文解读这份检验/检查报告，重点分析异常项可能对应的常见原因、"
            "需要结合哪些症状判断、建议复查或就诊科室。不要确诊，不要制造恐慌。\n"
            f"报告：{report}"
        )
        try:
            text = await llm.chat(
                [{"role": "system", "content": "你是医院报告解读 Agent，只做科普解释和就医建议。"}, {"role": "user", "content": prompt}],
                timeout=12,
            )
        except Exception:
            text = report_fallback(report, abnormal, department)
    else:
        text = report_fallback(report, abnormal, department)
    text = compliance_guard(text)
    return {"id": report_id, "department": department, "risk_level": risk_level, "analysis": text, "interpretation": text}


def report_fallback(report: Dict[str, Any], abnormal: List[Dict[str, Any]], department: str) -> str:
    if not abnormal:
        return f"报告《{report['name']}》当前未见明显异常项。若仍有不适，建议结合症状咨询内科或相关专科。"
    lines = [f"报告《{report['name']}》有 {len(abnormal)} 项需要关注："]
    for item in abnormal:
        if "白细胞" in item["name"] or "中性" in item["name"]:
            reason = "可能与感染、炎症、应激反应等有关，需要结合发热、咳嗽、咽痛、腹泻等症状判断"
        else:
            reason = "需要结合临床症状进一步判断"
        lines.append(f"- {item['name']}：{item['value']}{item.get('unit','')}，标记为{item['flag']}，{reason}。")
    lines.append(f"建议结合症状、体温、用药史和既往病史咨询「{department}」。异常指标不等于确诊，应由医生结合临床判断。")
    return "\n".join(lines)


# =========================================================
# 预约挂号排班
# =========================================================

def departments() -> List[str]:
    return DEPARTMENTS


def schedule_for_department(department: str, booked_counts: Dict[str, int] | None = None) -> Dict[str, Any]:
    booked_counts = booked_counts or {}
    titles = ["主任医师", "副主任医师", "主治医师"]
    surnames = ["王", "李", "张", "陈", "刘", "赵", "周", "黄", "林", "吴"]
    rows = []
    today = date.today()
    seed = sum(ord(ch) for ch in department)
    random.seed(seed)
    for day in range(7):
        visit_date = today + timedelta(days=day)
        for period, slots in [("上午", "08:30-11:30"), ("下午", "14:00-17:00")]:
            title = random.choice(titles)
            base_quota = random.randint(8, 32)
            doctor = f"{random.choice(surnames)}医生"
            key = appointment_key(department, doctor, visit_date.isoformat(), period, slots)
            remaining = max(base_quota - booked_counts.get(key, 0), 0)
            rows.append(
                {
                    "schedule_id": key,
                    "department": department,
                    "visit_date": visit_date.isoformat(),
                    "date": visit_date.isoformat(),
                    "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][visit_date.weekday()],
                    "period": period,
                    "time_slot": slots,
                    "doctor": doctor,
                    "doctor_title": title,
                    "title": title,
                    "remaining": remaining,
                    "quota": base_quota,
                    "fee": random.choice([25, 35, 50, 80]),
                }
            )
    return {"department": department, "schedule": rows}
