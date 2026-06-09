from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import SETTINGS
from app.core.process_logger import log_step
from app.schemas.chat import Evidence
from app.services.embedding_client import EmbeddingClient
from app.services.text_chunker import Chunk, chunk_markdown, chunk_text

logger = logging.getLogger(__name__)


class ChromaRAGService:
    """Chroma 向量检索服务。

    - `medical_kb`：通用医学知识库（来自 data/knowledge_base/*.md）
    - `user_reports`：用户上传报告的语义化摘要（按 user_id 做 metadata 隔离）
    检索接口签名与旧 RAGService.search 兼容（同步返回 List[Evidence]）。
    """

    def __init__(self):
        chroma_cfg = SETTINGS.get("chroma", {})
        self.persist_dir: str = chroma_cfg.get("persist_dir", "./data/chroma")
        self.kb_name: str = chroma_cfg.get("kb_collection", "medical_kb")
        self.reports_name: str = chroma_cfg.get("user_reports_collection", "user_reports")
        self.top_k_kb: int = int(chroma_cfg.get("top_k_kb", 6))
        self.top_k_reports: int = int(chroma_cfg.get("top_k_reports", 4))
        self.chunk_size: int = int(chroma_cfg.get("chunk_size", 400))
        self.chunk_overlap: int = int(chroma_cfg.get("chunk_overlap", 60))

        self.embed_client = EmbeddingClient()
        self.client = None
        self.kb_collection = None
        self.reports_collection = None
        self.available = False

        try:
            import chromadb

            Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
            self.client = chromadb.PersistentClient(path=self.persist_dir)
            self.kb_collection = self.client.get_or_create_collection(
                name=self.kb_name, metadata={"hnsw:space": "cosine"}
            )
            self.reports_collection = self.client.get_or_create_collection(
                name=self.reports_name, metadata={"hnsw:space": "cosine"}
            )
            self.available = True
            logger.info("Chroma initialized at %s", self.persist_dir)
        except Exception as exc:  # pragma: no cover - 启动期间日志
            logger.warning("Chroma init failed: %s. Fallback to legacy RAG.", exc)
            self.available = False

    # ---------- 写入 ----------

    async def add_documents(
        self,
        collection_name: str,
        texts: Sequence[str],
        metadatas: Sequence[Dict[str, Any]],
        ids: Optional[Sequence[str]] = None,
    ) -> List[str]:
        if not self.available:
            raise RuntimeError("Chroma not available")
        if not texts:
            return []
        log_step("chroma.add.begin", collection=collection_name, chunks=len(texts))
        collection = self._get_collection(collection_name)
        ids_list = list(ids) if ids else [str(uuid.uuid4()) for _ in texts]
        embeddings = await self.embed_client.embed_documents(list(texts))
        collection.add(
            ids=ids_list,
            documents=list(texts),
            metadatas=[dict(m) for m in metadatas],
            embeddings=embeddings,
        )
        log_step("chroma.add.done", collection=collection_name, chunks=len(texts))
        return ids_list

    async def add_kb_chunks(
        self,
        file_path: Path,
        chunks: List[Chunk],
        title: str,
        doc_type: str = "knowledge_base",
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        """通用入口：把已切好的 chunks 写入 medical_kb collection。"""
        if not chunks:
            return 0
        texts = [c.text for c in chunks]
        metadatas: List[Dict[str, Any]] = []
        ids: List[str] = []
        for c in chunks:
            meta: Dict[str, Any] = {
                "source": file_path.name,
                "title": title,
                "section": c.section or title,
                "doc_type": doc_type,
                "chunk_index": c.index,
            }
            if extra_meta:
                for k, v in extra_meta.items():
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        meta[k] = v
            metadatas.append(meta)
            ids.append(f"kb::{file_path.stem}::{doc_type}::{c.index}")
        try:
            self._get_collection(self.kb_name).delete(ids=ids)
        except Exception:
            pass
        await self.add_documents(self.kb_name, texts, metadatas, ids)
        return len(chunks)

    async def add_markdown_file(self, file_path: Path) -> int:
        text = Path(file_path).read_text(encoding="utf-8")
        title_line = text.splitlines()[0].lstrip("# ").strip() if text.strip() else file_path.stem
        chunks: List[Chunk] = chunk_markdown(text, self.chunk_size, self.chunk_overlap)
        return await self.add_kb_chunks(file_path, chunks, title=title_line, doc_type="markdown")

    def delete_kb_source(self, file_name: str) -> int:
        """按 source 文件名删除某份知识库文件的所有 chunk。"""
        if not self.available:
            return 0
        try:
            coll = self._get_collection(self.kb_name)
            before = coll.get(where={"source": file_name}, include=[])
            deleted = len(before.get("ids") or [])
            coll.delete(where={"source": file_name})
            return deleted
        except Exception as exc:
            logger.warning("delete_kb_source failed: %s", exc)
            return 0

    async def add_user_report(
        self,
        user_id: str,
        doc_id: str,
        doc_type: str,
        title: str,
        summary: str,
        full_text: str,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        """把用户上传报告写入 user_reports collection。

        策略：
        - 第 1 条：完整 summary（VLM 给出的医生友好摘要），priority=high
        - 后续：full_text 滑窗切片，priority=normal
        """
        if not self.available:
            raise RuntimeError("Chroma not available")
        chunks = chunk_text(full_text, self.chunk_size, self.chunk_overlap)
        texts: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        ids: List[str] = []

        base_meta = {
            "user_id": user_id,
            "doc_id": doc_id,
            "doc_type": doc_type,
            "title": title,
            "source": f"upload::{doc_id}",
        }
        if extra_meta:
            for k, v in extra_meta.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    base_meta[k] = v

        if summary.strip():
            texts.append(f"【{title} 摘要】\n{summary}")
            metadatas.append({**base_meta, "section": "summary", "priority": "high", "chunk_index": -1})
            ids.append(f"rpt::{doc_id}::summary")

        for c in chunks:
            texts.append(c.text)
            metadatas.append({**base_meta, "section": "detail", "priority": "normal", "chunk_index": c.index})
            ids.append(f"rpt::{doc_id}::{c.index}")

        if not texts:
            return 0
        await self.add_documents(self.reports_name, texts, metadatas, ids)
        return len(texts)

    # ---------- 检索 ----------

    async def query_kb(self, query: str, top_k: Optional[int] = None) -> List[Evidence]:
        return await self._query(self.kb_name, query, top_k or self.top_k_kb, where=None)

    async def query_user_reports(
        self,
        query: str,
        user_id: str,
        top_k: Optional[int] = None,
    ) -> List[Evidence]:
        return await self._query(
            self.reports_name,
            query,
            top_k or self.top_k_reports,
            where={"user_id": user_id},
        )

    async def _query(
        self,
        collection_name: str,
        query: str,
        top_k: int,
        where: Optional[Dict[str, Any]],
    ) -> List[Evidence]:
        if not self.available or not query.strip():
            return []
        try:
            log_step("chroma.query.begin", collection=collection_name, top_k=top_k, where=where)
            collection = self._get_collection(collection_name)
            embedding = await self.embed_client.embed_query(query)
            kwargs: Dict[str, Any] = {
                "query_embeddings": [embedding],
                "n_results": top_k,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            result = collection.query(**kwargs)
            log_step("chroma.query.done", collection=collection_name)
        except Exception as exc:
            logger.warning("Chroma query failed on %s: %s", collection_name, exc)
            return []

        evidences: List[Evidence] = []
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            score = float(1.0 - dist) if dist is not None else 0.0
            meta = meta or {}
            content = str(doc)
            title = str(meta.get("title", meta.get("section", collection_name)))
            if collection_name == self.reports_name:
                # 用户报告检索必须把时间、类型、doc_id 显式交给上层 Agent，
                # 否则模型容易把旧报告结论误当成当前报告。
                report_date = str(meta.get("report_date") or "")
                uploaded_at = str(meta.get("uploaded_at") or "")
                doc_type = str(meta.get("doc_type") or "")
                section = str(meta.get("section") or "")
                title = "｜".join([part for part in [title, report_date or uploaded_at, doc_type, section] if part])
                meta_header = "\n".join(
                    [
                        "【报告元数据】",
                        f"doc_id={meta.get('doc_id', '')}",
                        f"标题={meta.get('title', '')}",
                        f"报告类型={doc_type}",
                        f"报告日期={report_date or '未知'}",
                        f"上传时间={uploaded_at or '未知'}",
                        f"片段={section}",
                        f"优先级={meta.get('priority', '')}",
                    ]
                )
                content = f"{meta_header}\n\n{content}"
            evidences.append(
                Evidence(
                    source=str(meta.get("source", collection_name)),
                    title=title,
                    score=round(score, 4),
                    content=content[:2200],
                )
            )
        return evidences

    def search(self, query: str, top_k: Optional[int] = None) -> List[Evidence]:
        """同步包装，向后兼容旧接口（仅检索知识库）。"""
        if not self.available:
            return []
        return asyncio.run(self.query_kb(query, top_k))

    # ---------- 管理 ----------

    def stats(self) -> Dict[str, Any]:
        if not self.available:
            return {"available": False}
        return {
            "available": True,
            "persist_dir": self.persist_dir,
            "kb_count": self.kb_collection.count() if self.kb_collection else 0,
            "user_reports_count": self.reports_collection.count() if self.reports_collection else 0,
            "embedding_model": self.embed_client.model_name,
            "dimensions": self.embed_client.dimensions,
        }

    def delete_user_report(self, user_id: str, doc_id: str) -> int:
        if not self.available:
            return 0
        try:
            coll = self._get_collection(self.reports_name)
            coll.delete(where={"$and": [{"user_id": user_id}, {"doc_id": doc_id}]})
            return 1
        except Exception as exc:
            logger.warning("delete_user_report failed: %s", exc)
            return 0

    def delete_user_reports(self, user_id: str) -> int:
        if not self.available:
            return 0
        try:
            self.reports_collection.delete(where={"user_id": user_id})
            return 1
        except Exception as exc:
            logger.warning("delete_user_reports failed: %s", exc)
            return 0

    def delete_all_user_reports(self) -> int:
        if not self.available:
            return 0
        try:
            self.client.delete_collection(self.reports_name)
            self.reports_collection = self.client.get_or_create_collection(
                name=self.reports_name, metadata={"hnsw:space": "cosine"}
            )
            return 1
        except Exception as exc:
            logger.warning("delete_all_user_reports failed: %s", exc)
            return 0

    def _get_collection(self, name: str):
        if name == self.kb_name:
            return self.kb_collection
        if name == self.reports_name:
            return self.reports_collection
        return self.client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


# 单例
_INSTANCE: Optional[ChromaRAGService] = None


def get_chroma_service() -> ChromaRAGService:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = ChromaRAGService()
    return _INSTANCE
