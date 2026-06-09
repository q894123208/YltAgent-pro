"""用当前 embedding 模型重建 Chroma 中的用户报告记忆。

切换 embedding 模型后必须重建，否则旧向量和新查询向量不在同一空间。

用法（在 backend 目录）：
    python scripts/rebuild_user_reports.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_conn  # noqa: E402
from app.services.chroma_rag_service import get_chroma_service  # noqa: E402
from app.services.vlm_service import build_report_summary  # noqa: E402


async def main() -> None:
    chroma = get_chroma_service()
    if not chroma.available:
        print("[ERR] Chroma 不可用")
        return

    try:
        chroma.client.delete_collection(chroma.reports_name)
    except Exception:
        pass
    chroma.reports_collection = chroma.client.get_or_create_collection(
        name=chroma.reports_name,
        metadata={"hnsw:space": "cosine"},
    )

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT doc_id, user_id, session_id, doc_type, title, summary,
                   parsed_json, confidence, page_count
            FROM medical_documents
            ORDER BY created_at ASC
            """
        ).fetchall()

    total = 0
    for row in rows:
        parsed = json.loads(row["parsed_json"] or "{}")
        full_text = build_report_summary(parsed)
        chunks = await chroma.add_user_report(
            user_id=row["user_id"],
            doc_id=row["doc_id"],
            doc_type=row["doc_type"] or parsed.get("doc_type") or "other",
            title=row["title"] or parsed.get("title") or "医疗报告",
            summary=row["summary"] or parsed.get("summary") or "",
            full_text=full_text,
            extra_meta={
                "session_id": row["session_id"] or "",
                "page_count": row["page_count"] or 0,
                "confidence": row["confidence"] or 0.0,
                "report_date": parsed.get("report_date") or "",
                "suggested_department": parsed.get("suggested_department") or "",
            },
        )
        total += chunks
        print(f"+ {row['doc_id']} {row['title']} -> {chunks} chunks")

    print(f"[OK] rebuilt {len(rows)} reports, {total} chunks")


if __name__ == "__main__":
    asyncio.run(main())
