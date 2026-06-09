from __future__ import annotations

import asyncio
import math
from pathlib import Path
from threading import Lock
from typing import List, Sequence

import httpx

from app.core.config import SETTINGS
from app.core.process_logger import log_step


class EmbeddingClient:
    """Embedding client with explicit local/API provider selection.

    provider:
    - local: load local SentenceTransformer model from local_model_path
    - api / remote / dmxapi: call OpenAI-compatible /v1/embeddings API
    - auto: use local model when local_model_path exists, otherwise API
    """

    def __init__(self):
        cfg = SETTINGS.get("embedding") or {}
        self.provider: str = str(cfg.get("provider", "auto")).lower()
        self.api_key: str = cfg.get("api_key", "")
        self.base_url: str = cfg.get("base_url", "").rstrip("/")
        self.auth_scheme: str = str(cfg.get("auth_scheme", "bearer")).lower()
        self.model_name: str = cfg.get("model_name", "qwen3-embedding-8b")
        self.local_model_path: str = cfg.get("local_model_path", "")
        self.dimensions: int = int(cfg.get("dimensions", 1024))
        self.batch_size: int = int(cfg.get("batch_size", 16))
        self.query_instruction: str = cfg.get("query_instruction", "")
        self.device: str = cfg.get("device", "cuda")

        local_available = bool(self.local_model_path and Path(self.local_model_path).exists())
        self.use_local: bool = self.provider == "local" or (self.provider == "auto" and local_available)
        self.use_api: bool = self.provider in {"api", "remote", "dmxapi"} or (self.provider == "auto" and not self.use_local)
        self.enabled: bool = self.use_local or bool(
            self.use_api and self.api_key and self.base_url and self.api_key != "local"
        )

        self._local_model = None
        self._local_lock = Lock()

    def _format_query(self, text: str) -> str:
        if self.query_instruction:
            return f"{self.query_instruction}{text}"
        return text

    def _fit_dimensions(self, vector: List[float]) -> List[float]:
        """Truncate vectors for Matryoshka embeddings and normalize them again."""
        if not self.dimensions or len(vector) <= self.dimensions:
            return vector
        out = vector[: self.dimensions]
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    def _load_local_model(self):
        if self._local_model is not None:
            return self._local_model
        with self._local_lock:
            if self._local_model is not None:
                return self._local_model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "Local embedding requires sentence-transformers: pip install sentence-transformers"
                ) from exc
            self._local_model = SentenceTransformer(
                self.local_model_path,
                device=self.device,
                trust_remote_code=True,
            )
            return self._local_model

    def _encode_local_sync(self, inputs: List[str]) -> List[List[float]]:
        model = self._load_local_model()
        vectors = model.encode(
            inputs,
            batch_size=max(1, self.batch_size),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [self._fit_dimensions(v.astype(float).tolist()) for v in vectors]

    def preload(self) -> None:
        """Warm up local embedding model during application startup."""
        if self.use_local:
            self._encode_local_sync(["medical knowledge embedding warmup"])

    def _headers(self) -> dict[str, str]:
        token = self.api_key if self.auth_scheme == "raw" else f"Bearer {self.api_key}"
        return {"Authorization": token, "Content-Type": "application/json"}

    async def _post(self, inputs: List[str], timeout: float = 60.0, max_retries: int = 3) -> List[List[float]]:
        url = f"{self.base_url}/embeddings"
        payload = {"model": self.model_name, "input": inputs}
        if self.dimensions:
            payload["dimensions"] = self.dimensions

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=8.0, read=timeout, write=10.0, pool=5.0)
                ) as client:
                    resp = await client.post(url, headers=self._headers(), json=payload)
                    if resp.status_code >= 400 and "dimensions" in payload:
                        retry_payload = {k: v for k, v in payload.items() if k != "dimensions"}
                        resp = await client.post(url, headers=self._headers(), json=retry_payload)
                    if resp.status_code >= 400:
                        body = resp.text[:300]
                        raise RuntimeError(f"embedding HTTP {resp.status_code}: {body}")
                    data = resp.json()
                items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
                vectors = [self._fit_dimensions(item["embedding"]) for item in items]
                if not vectors or len(vectors) != len(inputs):
                    raise RuntimeError(f"embedding incomplete: got {len(vectors)} vs {len(inputs)}")
                return vectors
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0 + attempt * 1.5)
                    continue
        raise RuntimeError(f"embedding failed after {max_retries} retries: {type(last_exc).__name__}: {last_exc!r}")

    async def embed_documents(self, texts: Sequence[str], timeout: float = 60.0) -> List[List[float]]:
        if not self.enabled:
            raise RuntimeError(
                "EmbeddingClient is disabled. Check embedding.provider, api_key/base_url, or local_model_path."
            )
        clean_texts = [text if text else " " for text in texts]
        if self.use_local:
            log_step("embedding.local.begin", model=self.model_name, count=len(clean_texts))
            return await asyncio.to_thread(self._encode_local_sync, clean_texts)

        log_step("embedding.api.begin", provider=self.provider, model=self.model_name, count=len(clean_texts))
        results: List[List[float]] = []
        batch: List[str] = []
        for text in clean_texts:
            batch.append(text)
            if len(batch) >= self.batch_size:
                results.extend(await self._post(batch, timeout=timeout))
                batch = []
        if batch:
            results.extend(await self._post(batch, timeout=timeout))
        return results

    async def embed_query(self, text: str, timeout: float = 30.0) -> List[float]:
        if not self.enabled:
            raise RuntimeError("EmbeddingClient is disabled")
        query = self._format_query(text)
        if self.use_local:
            log_step("embedding.local.query", model=self.model_name)
            out = await asyncio.to_thread(self._encode_local_sync, [query])
            return out[0]
        log_step("embedding.api.query", provider=self.provider, model=self.model_name)
        out = await self._post([query], timeout=timeout)
        return out[0]

    def embed_documents_sync(self, texts: Sequence[str]) -> List[List[float]]:
        return asyncio.run(self.embed_documents(texts))

    def embed_query_sync(self, text: str) -> List[float]:
        return asyncio.run(self.embed_query(text))
