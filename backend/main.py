from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.chat import router as chat_router
from app.api.auth import router as auth_router
from app.api.metrics import router as metrics_router
from app.api.platform import router as platform_router
from app.api.upload import router as upload_router
from app.core.database import init_db
from app.core.process_logger import log_step, setup_process_logging
from app.services.chroma_rag_service import get_chroma_service

setup_process_logging()
logger = logging.getLogger(__name__)

app = FastAPI(title="医路通 AI 互联网医院智能服务平台", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    log_step("backend.startup.begin")
    init_db()
    log_step("backend.startup.database_ready")
    asyncio.create_task(preload_embedding_model())


async def preload_embedding_model():
    try:
        chroma = get_chroma_service()
        await asyncio.to_thread(chroma.embed_client.preload)
        log_step("embedding.preload.done", model=chroma.embed_client.model_name)
        logger.info("Embedding model preloaded")
    except Exception as exc:
        log_step("embedding.preload.skipped", error=type(exc).__name__)
        logger.warning("Embedding preload skipped: %s", exc)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


app.include_router(chat_router)
app.include_router(auth_router)
app.include_router(metrics_router)
app.include_router(platform_router)
app.include_router(upload_router)


if __name__ == "__main__":
    import uvicorn

    from app.core.config import SETTINGS

    uvicorn.run(app, host=SETTINGS["server"]["host"], port=int(SETTINGS["server"]["port"]))
