from __future__ import annotations

from fastapi import APIRouter

from app.core.config import SETTINGS
from app.core.database import list_sessions

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
async def metrics():
    sessions = list_sessions(limit=200)
    return {
        "session_count": len(sessions),
        "agent_count": 5,
        "skill_count": 5,
        "knowledge_docs": 6,
        "risk_distribution": [
            {"name": "低风险", "value": 46},
            {"name": "中风险", "value": 38},
            {"name": "高风险", "value": 16},
        ],
        "skill_usage": [
            {"skill": "症状分析", "count": 128},
            {"skill": "风险评估", "count": 121},
            {"skill": "知识检索", "count": 106},
            {"skill": "DeepResearch", "count": 39},
            {"skill": "合规检查", "count": 128},
        ],
    }


@router.get("/settings")
async def settings():
    llm = SETTINGS["llm"]
    features = SETTINGS["features"]
    return {
        "llm_enabled": bool(features.get("enable_llm")) and bool(llm.get("api_key")),
        "base_url": llm.get("base_url", ""),
        "model_name": llm.get("model_name", ""),
        "api_key_configured": bool(llm.get("api_key")),
        "deep_search_enabled": bool(features.get("enable_deep_search")),
        "rag_top_k": SETTINGS["rag"].get("top_k", 5),
    }
