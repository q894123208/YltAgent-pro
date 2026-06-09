from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from langchain_core.tools import StructuredTool

from app.services.chroma_rag_service import get_chroma_service
from app.services.deep_search import web_search
from app.services.skills import assess_risk, compliance_guard

KNOWN_INTERACTION_HINTS = [
    ("阿司匹林", "布洛芬", "NSAIDs 叠加可能增加胃肠道出血风险"),
    ("华法林", "阿司匹林", "抗凝+抗血小板叠加出血风险升高"),
    ("甲硝唑", "酒精", "可能引发双硫仑样反应"),
    ("头孢", "酒精", "部分头孢类可能引发双硫仑样反应"),
    ("对乙酰氨基酚", "复方感冒", "注意复方制剂重复成分导致过量"),
]


def _format_evidence(items: List[Any], limit: int = 5) -> str:
    if not items:
        return "未检索到相关内容。"
    lines: List[str] = []
    for idx, item in enumerate(items[:limit], start=1):
        if hasattr(item, "title"):
            title, content, source = item.title, item.content, item.source
        else:
            title = item.get("title", "资料")
            content = item.get("content", "")
            source = item.get("source", "")
        lines.append(f"{idx}. [{source}] {title}\n{str(content)[:500]}")
    return "\n\n".join(lines)


def _extract_drug_names(text: str) -> List[str]:
    if not text:
        return []
    patterns = [
        r"[\u4e00-\u9fff]{2,8}(?:片|胶囊|颗粒|口服液|注射液|缓释片|肠溶片)?",
        r"[A-Za-z][A-Za-z0-9\-]{2,}",
    ]
    found: List[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text):
            token = match.strip()
            if len(token) >= 2 and token not in found:
                found.append(token)
    return found[:12]


def build_medication_safety_tools(
    user_profile: Dict[str, Any],
    patient_context: Dict[str, Any] | None = None,
) -> List[StructuredTool]:
    patient_context = patient_context or {}

    async def search_medication_knowledge(query: str) -> str:
        """检索医学知识库中与用药安全、药物相互作用、禁忌相关的资料。"""
        chroma = get_chroma_service()
        q = f"用药安全 药物相互作用 禁忌 {query}".strip()
        if chroma.available:
            items = await chroma.query_kb(q, top_k=6)
            return _format_evidence(items)
        return "知识库暂不可用，请结合联网检索与患者信息谨慎回答。"

    async def get_patient_medication_profile() -> str:
        """获取当前用户的用药史、过敏史、慢病信息和基础人口学资料。"""
        profile = {
            "age": patient_context.get("age") or user_profile.get("age"),
            "gender": patient_context.get("gender") or user_profile.get("gender") or "",
            "chronic_diseases": patient_context.get("chronic_diseases") or user_profile.get("chronic_diseases") or "",
            "allergy_history": patient_context.get("allergy_history") or user_profile.get("allergy_history") or "",
            "medication_history": patient_context.get("medication_history") or user_profile.get("medication_history") or "",
            "display_name": user_profile.get("display_name") or user_profile.get("username") or "",
        }
        return json.dumps(profile, ensure_ascii=False)

    async def check_medication_interaction_rules(question: str) -> str:
        """基于规则检查问题中是否出现常见高风险药物组合或重复用药线索。"""
        text = question or ""
        drugs = _extract_drug_names(text)
        hits: List[str] = []
        lower = text.lower()
        for a, b, note in KNOWN_INTERACTION_HINTS:
            if a in text and b in text:
                hits.append(f"- {a} + {b}：{note}")
        risk = assess_risk(text)
        if risk.get("risk_level") == "高风险":
            hits.append(f"- 症状风险提示：{risk.get('reason')}；{risk.get('advice')}")
        if "孕妇" in text or "妊娠" in text or "哺乳" in text:
            hits.append("- 特殊人群：孕产期用药需咨询产科/药学门诊，避免自行选药。")
        if "儿童" in text or "小孩" in text or "宝宝" in text:
            hits.append("- 特殊人群：儿童用药需按体重/年龄评估，不建议照搬成人剂量。")
        payload = {
            "parsed_drug_like_terms": drugs,
            "rule_hits": hits or ["未发现预设规则中的明确高危组合，仍需结合说明书与个体差异判断。"],
            "keyword_risk": risk,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def web_search_medication_info(query: str) -> str:
        """联网检索药品安全性、相互作用或禁忌的公开资料。"""
        q = f"药品 用药安全 相互作用 {query}".strip()
        items = await web_search(q, limit=3, timeout=8.0)
        return _format_evidence(items)

    return [
        StructuredTool.from_function(
            coroutine=search_medication_knowledge,
            name="search_medication_knowledge",
            description="检索本地医学知识库中的用药安全、相互作用、禁忌相关内容。",
        ),
        StructuredTool.from_function(
            coroutine=get_patient_medication_profile,
            name="get_patient_medication_profile",
            description="读取当前用户用药史、过敏史、慢病和年龄性别等档案信息。",
        ),
        StructuredTool.from_function(
            coroutine=check_medication_interaction_rules,
            name="check_medication_interaction_rules",
            description="对问题文本做规则级用药风险扫描，识别常见药物组合风险与特殊人群提示。",
        ),
        StructuredTool.from_function(
            coroutine=web_search_medication_info,
            name="web_search_medication_info",
            description="联网搜索药品安全性、相互作用、禁忌等补充资料。",
        ),
    ]


def finalize_medication_answer(text: str) -> str:
    return compliance_guard(text or "")
