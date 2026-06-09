from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import os

import yaml


BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent


def resolve_project_path(path: str) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return (BACKEND_ROOT / candidate).resolve()


def load_config() -> Dict[str, Any]:
    config_path = BACKEND_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        config_path = BACKEND_ROOT / "config.example.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    llm = data.setdefault("llm", {})
    llm["api_key"] = os.getenv("MEDIX_API_KEY", llm.get("api_key", ""))
    llm["base_url"] = os.getenv("MEDIX_BASE_URL", llm.get("base_url", ""))
    llm["model_name"] = os.getenv("MEDIX_MODEL_NAME", llm.get("model_name", ""))
    if os.getenv("MEDIX_ENABLE_LLM"):
        data.setdefault("features", {})["enable_llm"] = os.getenv("MEDIX_ENABLE_LLM", "").lower() in {"1", "true", "yes", "on"}
    data["rag"]["knowledge_dir"] = str(resolve_project_path(data["rag"]["knowledge_dir"]))
    if data["rag"].get("triage_kb_dir"):
        data["rag"]["triage_kb_dir"] = str(resolve_project_path(data["rag"]["triage_kb_dir"]))
    db = data.setdefault("database", {})
    if "sqlite_path" in db:
        db["sqlite_path"] = str(resolve_project_path(db["sqlite_path"]))
    db["type"] = os.getenv("MEDIX_DB_TYPE", db.get("type", "sqlite"))
    db["postgres_url"] = os.getenv("MEDIX_POSTGRES_URL", db.get("postgres_url", ""))
    emb = data.setdefault("embedding", {})
    emb["api_key"] = os.getenv("MEDIX_EMBEDDING_API_KEY", emb.get("api_key", ""))
    emb["base_url"] = os.getenv("MEDIX_EMBEDDING_BASE_URL", emb.get("base_url", ""))
    emb["model_name"] = os.getenv("MEDIX_EMBEDDING_MODEL_NAME", emb.get("model_name", ""))
    if "chroma" in data:
        data["chroma"]["persist_dir"] = str(resolve_project_path(data["chroma"]["persist_dir"]))
    if "upload" in data:
        data["upload"]["storage_dir"] = str(resolve_project_path(data["upload"]["storage_dir"]))
    return data


SETTINGS = load_config()
