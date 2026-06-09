from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI

from app.core.config import SETTINGS


def get_chat_model(**overrides: Any) -> ChatOpenAI:
    """从 config.yaml 构建 LangChain ChatOpenAI，兼容 DMXAPI 等 OpenAI 兼容网关。"""
    cfg = SETTINGS.get("llm", {})
    if not SETTINGS.get("features", {}).get("enable_llm", True) or not cfg.get("api_key"):
        raise RuntimeError("LLM is disabled or api_key is empty")

    auth_scheme = str(cfg.get("auth_scheme", "auto")).lower()
    base_url = str(cfg.get("base_url", "") or None)
    api_key = str(cfg["api_key"])
    kwargs: dict[str, Any] = {
        "model": overrides.pop("model", cfg.get("model_name")),
        "api_key": api_key,
        "temperature": overrides.pop("temperature", cfg.get("temperature", 0.2)),
        "max_tokens": overrides.pop("max_tokens", cfg.get("max_tokens", 1800)),
        "timeout": overrides.pop("timeout", 90),
        **overrides,
    }
    if base_url:
        kwargs["base_url"] = base_url
    if auth_scheme == "raw" or "dmxapi.cn" in str(base_url or ""):
        kwargs["default_headers"] = {"Authorization": api_key}
    return ChatOpenAI(**kwargs)
