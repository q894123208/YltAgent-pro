from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List

from app.core.config import SETTINGS
from app.schemas.chat import Evidence


def tokenize(text: str) -> List[str]:
    chinese = re.findall(r"[\u4e00-\u9fff]{1,2}", text)
    latin = re.findall(r"[A-Za-z0-9]+", text.lower())
    return chinese + latin


class RAGService:
    def __init__(self):
        self.knowledge_dir = Path(SETTINGS["rag"]["knowledge_dir"])
        self.top_k = int(SETTINGS["rag"].get("top_k", 5))
        self.documents = self._load_docs()

    def _load_docs(self) -> List[Dict]:
        docs = []
        if not self.knowledge_dir.exists():
            return docs
        for path in self.knowledge_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            title = text.splitlines()[0].replace("#", "").strip() if text.splitlines() else path.stem
            chunks = [chunk.strip() for chunk in re.split(r"\n#{2,3}\s+", text) if chunk.strip()]
            for idx, chunk in enumerate(chunks):
                docs.append(
                    {
                        "source": path.name,
                        "title": title,
                        "chunk_id": idx,
                        "content": chunk[:1600],
                        "tokens": Counter(tokenize(chunk)),
                    }
                )
        return docs

    def search(self, query: str, top_k: int | None = None) -> List[Evidence]:
        q = Counter(tokenize(query))
        if not q:
            return []
        results = []
        q_norm = math.sqrt(sum(v * v for v in q.values()))
        for doc in self.documents:
            dot = sum(q[t] * doc["tokens"].get(t, 0) for t in q)
            d_norm = math.sqrt(sum(v * v for v in doc["tokens"].values())) or 1
            score = dot / (q_norm * d_norm or 1)
            if score > 0:
                results.append(
                    Evidence(
                        source=doc["source"],
                        title=doc["title"],
                        score=round(float(score), 4),
                        content=doc["content"],
                    )
                )
        results.sort(key=lambda x: x.score, reverse=True)
        return results[: top_k or self.top_k]
