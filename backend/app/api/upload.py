from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

import hashlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import SETTINGS
from app.core.auth import get_current_user
from app.core.process_logger import log_step
from app.core.database import (
    add_medical_document,
    delete_all_medical_documents,
    delete_all_report_sessions,
    delete_medical_document,
    delete_medical_documents_for_user,
    delete_report_sessions_for_user,
    get_medical_document,
    list_all_medical_documents,
    list_medical_documents,
    now_text,
)
from app.services.chroma_rag_service import get_chroma_service
from app.services.document_processor import file_to_images, is_supported as is_visual_supported
from app.services.text_extractor import extract_text, is_text_document
from app.services.vlm_service import VLMService, build_failed_report, build_report_summary

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/upload", tags=["upload"])

_vlm = VLMService()
_upload_cfg = SETTINGS.get("upload", {})
_storage_dir = Path(_upload_cfg.get("storage_dir", "./data/uploads"))
_max_size = int(_upload_cfg.get("max_file_mb", 20)) * 1024 * 1024
_allowed_exts = {f".{ext.lower().lstrip('.')}" for ext in _upload_cfg.get("allowed_extensions", [])}


# 简单进程内缓存：file sha256 -> 已解析的 doc_id（同一文件秒回）
_HASH_CACHE: dict[str, str] = {}


def _storage_bucket(suffix: str) -> str:
    """按源文件类型拆分存储目录，目录下再按 user_id 隔离。"""
    suffix = suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
        return "images"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "word"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "sheets"
    return "others"


def _fallback_text_report(extracted: str, suffix: str, error: Exception):
    summary = (extracted[:300] + "...") if len(extracted) > 300 else extracted
    return {
        "doc_type": f"text_{suffix.lstrip('.') or 'document'}",
        "title": "文本报告待复核",
        "summary": summary or f"远程模型暂时不可用，文本已抽取但未完成结构化解析：{type(error).__name__}",
        "items": [],
        "findings": "",
        "impression": "",
        "recommendations": [],
        "key_abnormalities": [],
        "suggested_department": "",
        "uncertain_fields": ["remote_model_unavailable"],
        "confidence": 0.25 if extracted else 0.0,
        "raw_text": extracted[:12000],
        "parse_status": "failed",
        "error": str(error)[:300],
    }


def _has_pdf_text_layer(extracted: str) -> bool:
    """只要 PDF 能提取到一定量真实文本，就按文字型 PDF 处理。"""
    cleaned = "\n".join(
        line for line in (extracted or "").splitlines()
        if "未提取到文字" not in line and not line.startswith("【PDF 第")
    ).strip()
    return len(cleaned) >= 30


async def _async_write_chroma(
    user_id: str,
    doc_id: str,
    doc_type: str,
    title: str,
    parsed_summary: str,
    full_text: str,
    session_id: Optional[str],
    page_count: int,
    confidence: float,
    report_date: str,
    suggested_department: str,
) -> int:
    chroma = get_chroma_service()
    if not chroma.available:
        return 0
    try:
        return await chroma.add_user_report(
            user_id=user_id,
            doc_id=doc_id,
            doc_type=doc_type,
            title=title,
            summary=parsed_summary,
            full_text=full_text,
            extra_meta={
                "session_id": session_id or "",
                "page_count": page_count,
                "confidence": confidence,
                "report_date": report_date,
                "suggested_department": suggested_department,
                "uploaded_at": now_text(),
            },
        )
    except Exception as exc:
        logger.warning("background chroma write failed: %s", exc)
        return 0


@router.post("/medical-document")
async def upload_medical_document(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    doc_type_hint: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
):
    started = time.perf_counter()
    stage_times: dict[str, int] = {}
    user_id = user["user_id"]
    log_step("upload.begin", user_id=user_id, filename=file.filename, session_id=session_id)
    if not SETTINGS.get("features", {}).get("enable_vlm_upload", True):
        raise HTTPException(status_code=403, detail="多模态上传未启用")
    if not _vlm.enabled:
        raise HTTPException(status_code=503, detail="VLM 未配置或不可用")

    filename = file.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if _allowed_exts and suffix not in _allowed_exts:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {suffix}")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(contents) > _max_size:
        raise HTTPException(status_code=413, detail=f"文件过大，限制 {_max_size // (1024*1024)} MB")

    # 哈希命中：同用户重复上传同一文件直接返回历史 doc
    file_hash = hashlib.sha256(contents).hexdigest()
    cache_key = f"{user_id}::{file_hash}"
    cached_doc_id = _HASH_CACHE.get(cache_key)
    if cached_doc_id:
        cached = get_medical_document(cached_doc_id)
        if cached and cached.get("user_id") == user_id:
            parsed = cached.get("parsed_json") or {}
            return {
                "status": parsed.get("parse_status") or "ok",
                "doc_id": cached_doc_id,
                "file_name": cached.get("file_name"),
                "page_count": cached.get("page_count") or 1,
                "title": cached.get("title"),
                "doc_type": cached.get("doc_type"),
                "summary": cached.get("summary") or parsed.get("summary") or "",
                "key_abnormalities": parsed.get("key_abnormalities") or [],
                "items": parsed.get("items") or [],
                "findings": parsed.get("findings") or "",
                "impression": parsed.get("impression") or "",
                "recommendations": parsed.get("recommendations") or [],
                "suggested_department": parsed.get("suggested_department") or "",
                "uncertain_fields": parsed.get("uncertain_fields") or [],
                "confidence": cached.get("confidence") or 0.0,
                "chroma_chunks": 0,
                "cached": True,
                "pipeline_steps": ["命中历史解析缓存", "读取 PostgreSQL 报告记录"],
                "parsed": parsed,
            }

    doc_id = uuid.uuid4().hex
    user_dir = _storage_dir / _storage_bucket(suffix) / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    saved_path = user_dir / f"{doc_id}{suffix}"
    saved_path.write_bytes(contents)
    stage_times["save_file_ms"] = int((time.perf_counter() - started) * 1000)
    log_step("upload.file_saved", user_id=user_id, doc_id=doc_id, path=saved_path, bytes=len(contents))

    is_text_doc = is_text_document(saved_path)
    if not (is_visual_supported(saved_path) or is_text_doc):
        raise HTTPException(status_code=400, detail="文件类型不在支持范围内")

    if suffix == ".pdf":
        try:
            extracted = extract_text(saved_path)
            stage_times["pdf_text_probe_ms"] = int((time.perf_counter() - started) * 1000)
            log_step("upload.pdf_text_probe", user_id=user_id, doc_id=doc_id, chars=len(extracted))
        except Exception as exc:
            logger.exception("pdf text probe failed")
            extracted = ""

        if _has_pdf_text_layer(extracted):
            try:
                log_step("upload.model_parse.begin", user_id=user_id, doc_id=doc_id, mode="pdf_text")
                parsed = await _vlm.analyze_text(extracted, doc_type_hint=doc_type_hint, extra_user_text=note)
                stage_times["model_parse_ms"] = int((time.perf_counter() - started) * 1000)
                log_step("upload.model_parse.done", user_id=user_id, doc_id=doc_id, confidence=parsed.get("confidence"))
            except Exception as exc:
                logger.exception("pdf text model analyze failed")
                log_step("upload.model_parse.failed", user_id=user_id, doc_id=doc_id, error=type(exc).__name__)
                parsed = _fallback_text_report(extracted, suffix, exc)
            summary_text = build_report_summary(parsed) + "\n\n【抽取原文】\n" + extracted[:12000]
            page_count = 1
        else:
            try:
                images = file_to_images(saved_path)
                stage_times["pdf_render_ms"] = int((time.perf_counter() - started) * 1000)
                log_step("upload.pdf_rendered_for_vlm", user_id=user_id, doc_id=doc_id, pages=len(images))
            except Exception as exc:
                logger.exception("pdf render failed")
                raise HTTPException(status_code=400, detail=f"PDF 渲染失败: {exc}")
            try:
                log_step("upload.model_parse.begin", user_id=user_id, doc_id=doc_id, mode="pdf_image_vlm")
                parsed = await _vlm.analyze(images, doc_type_hint=doc_type_hint, extra_user_text=note)
                stage_times["model_parse_ms"] = int((time.perf_counter() - started) * 1000)
                log_step("upload.model_parse.done", user_id=user_id, doc_id=doc_id, confidence=parsed.get("confidence"))
            except Exception as exc:
                logger.exception("VLM analyze scanned PDF failed")
                log_step("upload.model_parse.failed", user_id=user_id, doc_id=doc_id, error=type(exc).__name__)
                parsed = build_failed_report(exc, doc_type_hint=doc_type_hint)
            summary_text = build_report_summary(parsed)
            page_count = len(images)
    elif is_text_doc:
        # PDF/DOCX/XLSX/CSV：先确定性抽取文本，再交给远程多模态模型做医学结构化。
        try:
            extracted = extract_text(saved_path)
            stage_times["text_extract_ms"] = int((time.perf_counter() - started) * 1000)
            log_step("upload.text_extracted", user_id=user_id, doc_id=doc_id, chars=len(extracted))
        except Exception as exc:
            logger.exception("text_extract failed")
            raise HTTPException(status_code=400, detail=f"文档解析失败: {exc}")
        if not extracted.strip() or extracted.startswith("[未能"):
            raise HTTPException(status_code=422, detail=f"文档内容为空或不可读取：{extracted[:120]}")
        try:
            log_step("upload.model_parse.begin", user_id=user_id, doc_id=doc_id, mode="text")
            parsed = await _vlm.analyze_text(extracted, doc_type_hint=doc_type_hint, extra_user_text=note)
            stage_times["model_parse_ms"] = int((time.perf_counter() - started) * 1000)
            log_step("upload.model_parse.done", user_id=user_id, doc_id=doc_id, confidence=parsed.get("confidence"))
        except Exception as exc:
            logger.exception("text report model analyze failed")
            log_step("upload.model_parse.failed", user_id=user_id, doc_id=doc_id, error=type(exc).__name__)
            parsed = _fallback_text_report(extracted, suffix, exc)
        summary_text = build_report_summary(parsed) + "\n\n【抽取原文】\n" + extracted[:12000]
        page_count = 1
    else:
        try:
            images = file_to_images(saved_path)
            stage_times["image_encode_ms"] = int((time.perf_counter() - started) * 1000)
            log_step("upload.image_encoded", user_id=user_id, doc_id=doc_id, pages=len(images))
        except Exception as exc:
            logger.exception("file_to_images failed")
            raise HTTPException(status_code=400, detail=f"文件解析失败: {exc}")
        try:
            log_step("upload.model_parse.begin", user_id=user_id, doc_id=doc_id, mode="image")
            parsed = await _vlm.analyze(images, doc_type_hint=doc_type_hint, extra_user_text=note)
            stage_times["model_parse_ms"] = int((time.perf_counter() - started) * 1000)
            log_step("upload.model_parse.done", user_id=user_id, doc_id=doc_id, confidence=parsed.get("confidence"))
        except Exception as exc:
            logger.exception("VLM analyze failed")
            log_step("upload.model_parse.failed", user_id=user_id, doc_id=doc_id, error=type(exc).__name__)
            parsed = build_failed_report(exc, doc_type_hint=doc_type_hint)
        summary_text = build_report_summary(parsed)
        page_count = len(images)

    title = str(parsed.get("title") or parsed.get("doc_type") or "医疗报告")
    doc_type = str(parsed.get("doc_type") or "other")
    confidence = float(parsed.get("confidence") or 0.0)
    total_ms = int((time.perf_counter() - started) * 1000)
    parsed["processing"] = {"duration_ms": total_ms, "stage_times": stage_times}

    add_medical_document(
        doc_id=doc_id,
        user_id=user_id,
        session_id=session_id,
        file_name=filename,
        file_path=str(saved_path),
        doc_type=doc_type,
        title=title,
        summary=parsed.get("summary") or "",
        parsed=parsed,
        confidence=confidence,
        page_count=page_count,
    )
    log_step("upload.postgres_saved", user_id=user_id, doc_id=doc_id, doc_type=doc_type, title=title)

    log_step("upload.vectorize.begin", user_id=user_id, doc_id=doc_id)
    chroma_chunks = await _async_write_chroma(
        user_id=user_id,
        doc_id=doc_id,
        doc_type=doc_type,
        title=title,
        parsed_summary=parsed.get("summary") or "",
        full_text=summary_text,
        session_id=session_id,
        page_count=page_count,
        confidence=confidence,
        report_date=parsed.get("report_date") or "",
        suggested_department=parsed.get("suggested_department") or "",
    )
    log_step("upload.vectorize.done", user_id=user_id, doc_id=doc_id, chunks=chroma_chunks)
    total_ms = int((time.perf_counter() - started) * 1000)
    parsed["processing"]["duration_ms"] = total_ms
    parsed["processing"]["stage_times"]["vectorize_done_ms"] = total_ms
    _HASH_CACHE[cache_key] = doc_id

    return {
        "status": parsed.get("parse_status") or "ok",
        "doc_id": doc_id,
        "file_name": filename,
        "page_count": page_count,
        "title": title,
        "doc_type": doc_type,
        "summary": parsed.get("summary") or "",
        "key_abnormalities": parsed.get("key_abnormalities") or [],
        "items": parsed.get("items") or [],
        "findings": parsed.get("findings") or "",
        "impression": parsed.get("impression") or "",
        "recommendations": parsed.get("recommendations") or [],
        "suggested_department": parsed.get("suggested_department") or "",
        "uncertain_fields": parsed.get("uncertain_fields") or [],
        "confidence": confidence,
        "chroma_chunks": chroma_chunks,
        "duration_ms": total_ms,
        "cached": False,
        "pipeline_steps": [
            "保存原始文件",
            "解析医疗文档" if parsed.get("parse_status") != "failed" else "远程解析失败，已保存待复核",
            "写入 PostgreSQL 报告记录",
            f"向量化写入 Chroma：{chroma_chunks} 个片段",
        ],
        "parsed": parsed,
    }


@router.get("/medical-documents")
async def my_documents(limit: int = 50, user: dict = Depends(get_current_user)):
    return {"documents": list_medical_documents(user["user_id"], limit=limit)}


@router.get("/medical-document/{doc_id}")
async def document_detail(doc_id: str, user: dict = Depends(get_current_user)):
    item = get_medical_document(doc_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    if item.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="forbidden")
    return item


@router.delete("/medical-document/{doc_id}")
async def remove_document(doc_id: str, user: dict = Depends(get_current_user)):
    item = get_medical_document(doc_id)
    ok = delete_medical_document(doc_id, user["user_id"])
    if ok:
        get_chroma_service().delete_user_report(user["user_id"], doc_id)
        if item and item.get("file_path"):
            try:
                Path(item["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass
    return {"ok": ok}


@router.delete("/admin/my-reports")
async def delete_my_reports(user: dict = Depends(get_current_user)):
    docs = list_medical_documents(user["user_id"], limit=10000)
    for doc in docs:
        item = get_medical_document(doc["doc_id"])
        if item and item.get("file_path"):
            try:
                Path(item["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass
    deleted_sessions = delete_report_sessions_for_user(user["user_id"])
    deleted = delete_medical_documents_for_user(user["user_id"])
    vector_deleted = get_chroma_service().delete_user_reports(user["user_id"])
    return {"deleted": deleted, "deleted_sessions": deleted_sessions, "vector_deleted": vector_deleted}


@router.delete("/admin/all-reports")
async def delete_all_reports(user: dict = Depends(get_current_user)):
    docs = list_all_medical_documents()
    for doc in docs:
        if doc.get("file_path"):
            try:
                Path(doc["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass
    deleted_sessions = delete_all_report_sessions()
    deleted = delete_all_medical_documents()
    vector_deleted = get_chroma_service().delete_all_user_reports()
    # 清理空上传目录即可；失败不影响数据库删除。
    try:
        shutil.rmtree(_storage_dir, ignore_errors=True)
        _storage_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return {"deleted": deleted, "deleted_sessions": deleted_sessions, "vector_deleted": vector_deleted}


@router.get("/medical-document/{doc_id}/raw")
async def document_raw(doc_id: str, user: dict = Depends(get_current_user)):
    item = get_medical_document(doc_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    if item.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="forbidden")
    file_path = Path(item["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="file missing on disk")
    suffix = file_path.suffix.lower()
    media = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media, filename=item.get("file_name") or file_path.name)


@router.get("/chroma/stats")
async def chroma_stats():
    return get_chroma_service().stats()
