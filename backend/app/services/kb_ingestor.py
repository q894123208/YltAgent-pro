"""知识库多格式入库器。

支持：
- .md / .txt          → 直接抽文字、切片、embedding
- .pdf（文本型）       → PyMuPDF 抽文字
- .pdf（扫描影像型）   → VLM 逐页描述（兜底）
- 图片 .png/.jpg/...   → VLM 描述图像 / 报告内容
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Tuple

from app.services.chroma_rag_service import ChromaRAGService
from app.services.document_processor import file_to_images
from app.services.text_chunker import Chunk, chunk_markdown, chunk_text
from app.services.text_extractor import TEXT_DOC_EXTS, extract_text
from app.services.vlm_service import VLMService, build_report_summary

logger = logging.getLogger(__name__)

TEXT_EXTS = {".md", ".markdown", ".txt"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
OFFICE_EXTS = TEXT_DOC_EXTS  # .docx .doc .xlsx .xls .csv

ALL_SUPPORTED = TEXT_EXTS | PDF_EXTS | IMAGE_EXTS | OFFICE_EXTS


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in ALL_SUPPORTED


def detect_pdf_kind(path: Path, text_threshold: int = 80) -> str:
    """返回 'text' 还是 'scan'。前 3 页能抽到 ≥ threshold 个字符就算 text。"""
    try:
        import fitz
        with fitz.open(str(path)) as doc:
            total = ""
            for i, page in enumerate(doc):
                if i >= 3:
                    break
                total += page.get_text("text") or ""
            return "text" if len(total.strip()) >= text_threshold else "scan"
    except Exception:
        return "scan"


def _pdf_extract_text(path: Path) -> str:
    import fitz
    parts: List[str] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            txt = (page.get_text("text") or "").strip()
            if txt:
                parts.append(txt)
    return "\n\n".join(parts)


async def _vlm_describe_images(vlm: VLMService, images, file_label: str) -> str:
    """让 VLM 把图片/扫描页"描述+解读"成长文本，用于入库。"""
    parsed = await vlm.analyze(
        images,
        doc_type_hint=f"知识库文档：{file_label}（用于医学问诊检索）",
        extra_user_text="请尽可能完整地把图片中的文字、要点、临床意义都抽出来，便于后续语义检索。",
    )
    # 用 build_report_summary 拼出长摘要 + 把 findings/impression/items 都展开
    summary = build_report_summary(parsed)
    if parsed.get("raw"):
        summary += "\n\n[原始模型输出]\n" + str(parsed["raw"])[:3000]
    return summary or (parsed.get("summary") or "")


async def ingest_text_file(svc: ChromaRAGService, path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        return 0
    title_line = text.splitlines()[0].lstrip("# ").strip() if text.strip() else path.stem
    if path.suffix.lower() in {".md", ".markdown"}:
        chunks = chunk_markdown(text, svc.chunk_size, svc.chunk_overlap)
        doc_type = "markdown"
    else:
        chunks = chunk_text(text, svc.chunk_size, svc.chunk_overlap)
        doc_type = "text"
    return await svc.add_kb_chunks(path, chunks, title=title_line, doc_type=doc_type)


async def ingest_pdf_file(svc: ChromaRAGService, path: Path) -> Tuple[int, str]:
    """返回 (chunks 数, 模式 text/scan)。"""
    kind = detect_pdf_kind(path)
    if kind == "text":
        text = _pdf_extract_text(path)
        if not text.strip():
            kind = "scan"
        else:
            chunks = chunk_text(text, svc.chunk_size, svc.chunk_overlap)
            n = await svc.add_kb_chunks(path, chunks, title=path.stem, doc_type="pdf_text")
            return n, "text"
    # 走 VLM
    vlm = VLMService()
    if not vlm.enabled:
        logger.warning("scanned PDF skipped (VLM disabled): %s", path)
        return 0, "scan_skipped"
    images = file_to_images(path)
    described = await _vlm_describe_images(vlm, images, path.name)
    if not described.strip():
        return 0, "scan_empty"
    chunks = chunk_text(described, svc.chunk_size, svc.chunk_overlap)
    n = await svc.add_kb_chunks(
        path,
        chunks,
        title=path.stem,
        doc_type="pdf_scan",
        extra_meta={"page_count": len(images)},
    )
    return n, "scan"


async def ingest_office_file(svc: ChromaRAGService, path: Path) -> int:
    """处理 docx/doc/xlsx/xls/csv：用 text_extractor 抽文本后切片入库。"""
    text = extract_text(path)
    if not text.strip() or text.startswith("[未能"):
        logger.warning("office extract empty/failed: %s", path)
        return 0
    chunks = chunk_text(text, svc.chunk_size, svc.chunk_overlap)
    doc_type = path.suffix.lower().lstrip(".")
    return await svc.add_kb_chunks(path, chunks, title=path.stem, doc_type=f"office_{doc_type}")


async def ingest_image_file(svc: ChromaRAGService, path: Path) -> int:
    vlm = VLMService()
    if not vlm.enabled:
        logger.warning("image skipped (VLM disabled): %s", path)
        return 0
    images = file_to_images(path)
    described = await _vlm_describe_images(vlm, images, path.name)
    if not described.strip():
        return 0
    chunks = chunk_text(described, svc.chunk_size, svc.chunk_overlap)
    return await svc.add_kb_chunks(
        path,
        chunks,
        title=path.stem,
        doc_type="image",
        extra_meta={"width": images[0].width, "height": images[0].height},
    )


async def ingest_file(svc: ChromaRAGService, path: Path) -> dict:
    """按扩展名分发。返回汇总信息。"""
    suffix = path.suffix.lower()
    if suffix in TEXT_EXTS:
        n = await ingest_text_file(svc, path)
        return {"file": path.name, "type": "text", "chunks": n}
    if suffix in PDF_EXTS:
        n, mode = await ingest_pdf_file(svc, path)
        return {"file": path.name, "type": f"pdf:{mode}", "chunks": n}
    if suffix in IMAGE_EXTS:
        n = await ingest_image_file(svc, path)
        return {"file": path.name, "type": "image", "chunks": n}
    if suffix in OFFICE_EXTS:
        n = await ingest_office_file(svc, path)
        return {"file": path.name, "type": f"office:{suffix.lstrip('.')}", "chunks": n}
    return {"file": path.name, "type": "unsupported", "chunks": 0}
