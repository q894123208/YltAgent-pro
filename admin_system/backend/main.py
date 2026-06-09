from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import SETTINGS  # noqa: E402
from app.core.database import (  # noqa: E402
    USE_POSTGRES,
    _execute,
    _ph,
    get_conn,
    get_medical_document,
)
from app.services.chroma_rag_service import get_chroma_service  # noqa: E402
from app.services.kb_ingestor import ALL_SUPPORTED, ingest_file  # noqa: E402


app = FastAPI(title="Medical Agent Admin API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


ADMIN_SESSIONS: set[str] = set()


def admin_username() -> str:
    return os.getenv("MEDIX_ADMIN_USERNAME") or str(
        SETTINGS.get("admin", {}).get("username") or "admin"
    )


def admin_password() -> str:
    return os.getenv("MEDIX_ADMIN_PASSWORD") or str(
        SETTINGS.get("admin", {}).get("password") or "change-me-admin-password"
    )


def require_admin(x_admin_session: str | None = Header(default=None)) -> None:
    if not x_admin_session or x_admin_session not in ADMIN_SESSIONS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin session expired")


def rows_to_dicts(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [dict(row) for row in rows]


def user_identity_keys(conn, user_id: str) -> List[str]:
    """兼容旧数据：有些历史预约可能存的是手机号/username，而不是 user_id。"""
    row = _execute(conn, "SELECT user_id, username, phone FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return [user_id]
    data = dict(row)
    keys = [data.get("user_id"), data.get("username"), data.get("phone")]
    return [str(item) for item in dict.fromkeys(keys) if item]


def decode_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def safe_unlink(path_text: str | None) -> bool:
    if not path_text:
        return False
    upload_root = Path(SETTINGS.get("upload", {}).get("storage_dir", PROJECT_ROOT / "data" / "uploads")).resolve()
    path = Path(path_text).resolve()
    try:
        path.relative_to(upload_root)
    except ValueError:
        return False
    if path.exists() and path.is_file():
        path.unlink()
        return True
    return False


def delete_doc_rows(doc_rows: List[Dict[str, Any]]) -> Dict[str, int]:
    chroma = get_chroma_service()
    files_deleted = 0
    vectors_deleted = 0
    rows_deleted = 0
    with get_conn() as conn:
        for doc in doc_rows:
            doc_id = doc["doc_id"]
            user_id = doc["user_id"]
            if safe_unlink(doc.get("file_path")):
                files_deleted += 1
            vectors_deleted += chroma.delete_user_report(user_id, doc_id)
            _execute(conn, "DELETE FROM message_attachments WHERE doc_id=?", (doc_id,))
            cur = _execute(conn, "DELETE FROM medical_documents WHERE doc_id=?", (doc_id,))
            rows_deleted += int(cur.rowcount or 0)
        conn.commit()
    return {"rows_deleted": rows_deleted, "files_deleted": files_deleted, "vectors_deleted": vectors_deleted}


def delete_sessions(session_ids: List[str]) -> int:
    if not session_ids:
        return 0
    placeholders = _ph(len(session_ids))
    with get_conn() as conn:
        conn.execute(f"DELETE FROM message_attachments WHERE session_id IN ({placeholders})", session_ids)
        conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", session_ids)
        conn.execute(f"DELETE FROM encounters WHERE session_id IN ({placeholders})", session_ids)
        cur = conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", session_ids)
        conn.commit()
        return int(cur.rowcount or 0)


class LoginPayload(BaseModel):
    username: str
    password: str


class UserUpdatePayload(BaseModel):
    username: str | None = None
    display_name: str | None = None


class BatchDocDeletePayload(BaseModel):
    doc_ids: List[str] = []


class BatchSessionDeletePayload(BaseModel):
    session_ids: List[str] = []


@app.post("/api/admin/login")
async def login(payload: LoginPayload):
    if payload.username != admin_username() or payload.password != admin_password():
        raise HTTPException(status_code=401, detail="invalid username or password")
    session = secrets.token_urlsafe(32)
    ADMIN_SESSIONS.add(session)
    return {"ok": True, "session": session, "username": payload.username}


@app.post("/api/admin/logout", dependencies=[Depends(require_admin)])
async def logout(x_admin_session: str | None = Header(default=None)):
    if x_admin_session:
        ADMIN_SESSIONS.discard(x_admin_session)
    return {"ok": True}


@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
async def stats():
    chroma = get_chroma_service()
    with get_conn() as conn:
        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])

        return {
            "database": {
                "type": "postgresql" if USE_POSTGRES else "sqlite",
                "users": count("users"),
                "sessions": count("sessions"),
                "messages": count("messages"),
                "reports": count("medical_documents"),
                "encounters": count("encounters"),
                "appointments": count("appointments"),
            },
            "chroma": chroma.stats(),
        }


@app.get("/api/admin/stats/breakdown", dependencies=[Depends(require_admin)])
async def stats_breakdown():
    """供仪表盘可视化用：场景分布、报告类型分布、近 14 天新增。"""
    with get_conn() as conn:
        scene_rows = _execute(
            conn,
            "SELECT COALESCE(scene,'') AS scene, COUNT(*) AS c FROM sessions GROUP BY scene ORDER BY c DESC",
        ).fetchall()
        doc_rows = _execute(
            conn,
            "SELECT COALESCE(doc_type,'') AS doc_type, COUNT(*) AS c FROM medical_documents GROUP BY doc_type ORDER BY c DESC",
        ).fetchall()
        # 近 14 天新增（按 created_at 前 10 位日期分组）
        date_expr = "TO_CHAR(created_at::date,'YYYY-MM-DD')" if USE_POSTGRES else "substr(created_at,1,10)"
        sessions_daily = _execute(
            conn,
            f"SELECT {date_expr} AS d, COUNT(*) AS c FROM sessions GROUP BY d ORDER BY d DESC LIMIT 14",
        ).fetchall()
        reports_daily = _execute(
            conn,
            f"SELECT {date_expr} AS d, COUNT(*) AS c FROM medical_documents GROUP BY d ORDER BY d DESC LIMIT 14",
        ).fetchall()
        messages_daily = _execute(
            conn,
            f"SELECT {date_expr} AS d, COUNT(*) AS c FROM messages GROUP BY d ORDER BY d DESC LIMIT 14",
        ).fetchall()
    return {
        "scenes": rows_to_dicts(scene_rows),
        "doc_types": rows_to_dicts(doc_rows),
        "sessions_daily": list(reversed(rows_to_dicts(sessions_daily))),
        "reports_daily": list(reversed(rows_to_dicts(reports_daily))),
        "messages_daily": list(reversed(rows_to_dicts(messages_daily))),
    }


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
async def list_users(q: str = "", limit: int = 20, offset: int = 0):
    keyword = f"%{q.strip()}%"
    base_sql = """
        SELECT u.user_id, u.username, u.phone, u.id_number,
               u.display_name, u.created_at, u.last_login_at,
               h.age, h.gender, h.address
        FROM users u
        LEFT JOIN health_profiles h ON h.user_id = u.user_id
    """
    with get_conn() as conn:
        if q.strip():
            total = int(_execute(
                conn,
                "SELECT COUNT(*) AS c FROM users WHERE username LIKE ? OR display_name LIKE ? OR user_id LIKE ? OR phone LIKE ? OR id_number LIKE ?",
                (keyword, keyword, keyword, keyword, keyword),
            ).fetchone()["c"])
            rows = _execute(
                conn,
                base_sql + """
                WHERE u.username LIKE ? OR u.display_name LIKE ? OR u.user_id LIKE ? OR u.phone LIKE ? OR u.id_number LIKE ?
                ORDER BY u.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (keyword, keyword, keyword, keyword, keyword, limit, offset),
            ).fetchall()
        else:
            total = int(_execute(conn, "SELECT COUNT(*) AS c FROM users").fetchone()["c"])
            rows = _execute(
                conn,
                base_sql + " ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return {"users": rows_to_dicts(rows), "total": total}


@app.get("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
async def user_detail(user_id: str):
    with get_conn() as conn:
        user = _execute(
            conn,
            """
            SELECT u.user_id, u.username, u.phone, u.id_number,
                   u.display_name, u.created_at, u.last_login_at,
                   h.age, h.gender, h.address, h.chronic_diseases, h.allergy_history, h.medication_history
            FROM users u
            LEFT JOIN health_profiles h ON h.user_id = u.user_id
            WHERE u.user_id=?
            """,
            (user_id,),
        ).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        user_keys = user_identity_keys(conn, user_id)
        key_placeholders = _ph(len(user_keys))
        counts = {
            "reports": _execute(conn, "SELECT COUNT(*) AS c FROM medical_documents WHERE user_id=?", (user_id,)).fetchone()["c"],
            "sessions": _execute(conn, "SELECT COUNT(*) AS c FROM sessions WHERE user_id=?", (user_id,)).fetchone()["c"],
            "encounters": _execute(conn, "SELECT COUNT(*) AS c FROM encounters WHERE user_id=?", (user_id,)).fetchone()["c"],
            "appointments": _execute(
                conn,
                f"SELECT COUNT(*) AS c FROM appointments WHERE user_id IN ({key_placeholders})",
                user_keys,
            ).fetchone()["c"],
        }
        appointments = _execute(
            conn,
            f"""
            SELECT id, user_id, department, doctor, doctor_title, visit_date, period, time_slot, status, created_at
            FROM appointments WHERE user_id IN ({key_placeholders})
            ORDER BY visit_date DESC, id DESC LIMIT 20
            """,
            user_keys,
        ).fetchall()
    return {"user": dict(user), "counts": counts, "appointments": rows_to_dicts(appointments)}


@app.get("/api/admin/users/{user_id}/appointments", dependencies=[Depends(require_admin)])
async def list_user_appointments(user_id: str, limit: int = 50, offset: int = 0):
    with get_conn() as conn:
        user_keys = user_identity_keys(conn, user_id)
        key_placeholders = _ph(len(user_keys))
        params = [*user_keys]
        total = int(
            _execute(
                conn,
                f"SELECT COUNT(*) AS c FROM appointments WHERE user_id IN ({key_placeholders})",
                params,
            ).fetchone()["c"]
        )
        rows = _execute(
            conn,
            f"""
            SELECT id, user_id, department, doctor, doctor_title, visit_date, period, time_slot, status, created_at
            FROM appointments WHERE user_id IN ({key_placeholders})
            ORDER BY visit_date DESC, id DESC LIMIT ? OFFSET ?
            """,
            [*user_keys, limit, offset],
        ).fetchall()
    return {"appointments": rows_to_dicts(rows), "total": total}


@app.patch("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
async def update_user(user_id: str, payload: UserUpdatePayload):
    fields: List[str] = []
    params: List[Any] = []
    if payload.username is not None:
        fields.append("username=?")
        params.append(payload.username)
    if payload.display_name is not None:
        fields.append("display_name=?")
        params.append(payload.display_name)
    if not fields:
        return {"ok": True, "updated": 0}
    params.append(user_id)
    with get_conn() as conn:
        cur = _execute(conn, f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", params)
        conn.commit()
    return {"ok": True, "updated": int(cur.rowcount or 0)}


@app.delete("/api/admin/users/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: str, cascade: bool = True):
    with get_conn() as conn:
        user = _execute(conn, "SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="user not found")
        docs = rows_to_dicts(_execute(conn, "SELECT * FROM medical_documents WHERE user_id=?", (user_id,)).fetchall())
        sessions = rows_to_dicts(_execute(conn, "SELECT id FROM sessions WHERE user_id=?", (user_id,)).fetchall())
    result: Dict[str, Any] = {}
    if cascade:
        result["reports"] = delete_doc_rows(docs)
        result["sessions_deleted"] = delete_sessions([s["id"] for s in sessions])
        with get_conn() as conn:
            _execute(conn, "DELETE FROM encounters WHERE user_id=?", (user_id,))
            _execute(conn, "DELETE FROM appointments WHERE user_id=?", (user_id,))
            _execute(conn, "DELETE FROM health_profiles WHERE user_id=?", (user_id,))
            conn.commit()
    with get_conn() as conn:
        cur = _execute(conn, "DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()
    result["users_deleted"] = int(cur.rowcount or 0)
    return {"ok": True, **result}


@app.get("/api/admin/reports", dependencies=[Depends(require_admin)])
async def list_reports(user_id: str = "", q: str = "", limit: int = 20, offset: int = 0):
    where = []
    params: List[Any] = []
    if user_id:
        where.append("d.user_id=?")
        params.append(user_id)
    if q:
        like = f"%{q}%"
        where.append("(d.title LIKE ? OR d.file_name LIKE ? OR d.doc_type LIKE ? OR d.doc_id LIKE ?)")
        params.extend([like, like, like, like])
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    count_sql = f"SELECT COUNT(*) AS c FROM medical_documents d LEFT JOIN users u ON u.user_id=d.user_id {where_sql}"
    list_params = list(params) + [limit, offset]
    sql = f"""
        SELECT d.doc_id, d.user_id, u.display_name, d.session_id, d.file_name, d.file_path,
               d.doc_type, d.title, d.summary, d.confidence, d.page_count, d.created_at
        FROM medical_documents d
        LEFT JOIN users u ON u.user_id = d.user_id
        {where_sql}
        ORDER BY d.created_at DESC
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        total = int(_execute(conn, count_sql, params).fetchone()["c"])
        rows = _execute(conn, sql, list_params).fetchall()
    return {"reports": rows_to_dicts(rows), "total": total}


@app.get("/api/admin/reports/{doc_id}", dependencies=[Depends(require_admin)])
async def report_detail(doc_id: str):
    doc = get_medical_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="report not found")
    return {"report": doc}


@app.delete("/api/admin/reports/{doc_id}", dependencies=[Depends(require_admin)])
async def delete_report(doc_id: str):
    with get_conn() as conn:
        docs = rows_to_dicts(_execute(conn, "SELECT * FROM medical_documents WHERE doc_id=?", (doc_id,)).fetchall())
    if not docs:
        raise HTTPException(status_code=404, detail="report not found")
    return {"ok": True, **delete_doc_rows(docs)}


@app.post("/api/admin/reports/batch-delete", dependencies=[Depends(require_admin)])
async def batch_delete_reports(payload: BatchDocDeletePayload):
    if not payload.doc_ids:
        return {"ok": True, "rows_deleted": 0, "files_deleted": 0, "vectors_deleted": 0}
    placeholders = _ph(len(payload.doc_ids))
    with get_conn() as conn:
        docs = rows_to_dicts(conn.execute(f"SELECT * FROM medical_documents WHERE doc_id IN ({placeholders})", payload.doc_ids).fetchall())
    return {"ok": True, **delete_doc_rows(docs)}


@app.delete("/api/admin/users/{user_id}/reports", dependencies=[Depends(require_admin)])
async def delete_user_reports(user_id: str):
    with get_conn() as conn:
        docs = rows_to_dicts(_execute(conn, "SELECT * FROM medical_documents WHERE user_id=?", (user_id,)).fetchall())
    return {"ok": True, **delete_doc_rows(docs)}


@app.get("/api/admin/sessions", dependencies=[Depends(require_admin)])
async def list_admin_sessions(user_id: str = "", scene: str = "", limit: int = 20, offset: int = 0):
    where = []
    params: List[Any] = []
    if user_id:
        where.append("s.user_id=?")
        params.append(user_id)
    if scene:
        where.append("s.scene=?")
        params.append(scene)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    count_sql = f"SELECT COUNT(*) AS c FROM sessions s {where_sql}"
    list_params = list(params) + [limit, offset]
    sql = f"""
        SELECT s.id, s.user_id, u.username, s.scene, s.title, s.created_at, s.updated_at,
               COUNT(m.id) AS message_count
        FROM sessions s
        LEFT JOIN users u ON u.user_id = s.user_id
        LEFT JOIN messages m ON m.session_id = s.id
        {where_sql}
        GROUP BY s.id, s.user_id, u.username, s.scene, s.title, s.created_at, s.updated_at
        ORDER BY s.updated_at DESC
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        total = int(_execute(conn, count_sql, params).fetchone()["c"])
        rows = _execute(conn, sql, list_params).fetchall()
    return {"sessions": rows_to_dicts(rows), "total": total}


@app.post("/api/admin/sessions/batch-delete", dependencies=[Depends(require_admin)])
async def batch_delete_sessions(payload: BatchSessionDeletePayload):
    if not payload.session_ids:
        return {"ok": True, "sessions_deleted": 0}
    return {"ok": True, "sessions_deleted": delete_sessions(payload.session_ids)}


@app.get("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def session_detail(session_id: str):
    with get_conn() as conn:
        session = _execute(conn, "SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        messages = _execute(
            conn,
            "SELECT id, role, content, metadata, created_at FROM messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    items = []
    for row in messages:
        item = dict(row)
        item["metadata"] = decode_json(item.get("metadata"), {})
        items.append(item)
    return {"session": dict(session), "messages": items}


@app.delete("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def delete_session(session_id: str):
    return {"ok": True, "sessions_deleted": delete_sessions([session_id])}


@app.delete("/api/admin/users/{user_id}/sessions", dependencies=[Depends(require_admin)])
async def delete_user_sessions(user_id: str):
    with get_conn() as conn:
        rows = _execute(conn, "SELECT id FROM sessions WHERE user_id=?", (user_id,)).fetchall()
    return {"ok": True, "sessions_deleted": delete_sessions([row["id"] for row in rows])}


@app.get("/api/admin/kb/stats", dependencies=[Depends(require_admin)])
async def kb_stats():
    kb_dir = Path(SETTINGS["rag"]["knowledge_dir"])
    triage_dir = Path(SETTINGS.get("rag", {}).get("triage_kb_dir", ""))
    files = []
    roots = [(kb_dir, "通用知识库"), (triage_dir, "分诊知识库")]
    for root, kb_type in roots:
        if root and root.exists():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.lower() in ALL_SUPPORTED:
                    files.append(
                        {
                            "name": path.name,
                            "path": str(path),
                            "size": path.stat().st_size,
                            "kb_type": kb_type,
                        }
                    )
    return {"files": files, "chroma": get_chroma_service().stats()}


@app.post("/api/admin/kb/upload", dependencies=[Depends(require_admin)])
async def upload_kb_file(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALL_SUPPORTED:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {suffix}")
    target_dir = Path(SETTINGS["rag"]["knowledge_dir"]) / "admin_uploads"
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex}_{Path(file.filename or 'kb').name}"
    target = target_dir / safe_name
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    info = await ingest_file(get_chroma_service(), target)
    return {"ok": True, "file": str(target), "chunks": info.get("chunks", 0), "type": info.get("type")}


@app.post("/api/admin/kb/rebuild", dependencies=[Depends(require_admin)])
async def rebuild_kb(reset: bool = True):
    from scripts.build_kb import run

    await run(reset=reset)
    return {"ok": True, "chroma": get_chroma_service().stats()}


@app.delete("/api/admin/kb/file", dependencies=[Depends(require_admin)])
async def delete_kb_file(path: str):
    """删除单个知识库文件并清理它在 Chroma medical_kb 中的向量。"""
    kb_dir = Path(SETTINGS["rag"]["knowledge_dir"]).resolve()
    triage_dir = Path(SETTINGS.get("rag", {}).get("triage_kb_dir", "")).resolve() if SETTINGS.get("rag", {}).get("triage_kb_dir") else None
    target = Path(path).resolve()
    allowed = False
    for root in [kb_dir, triage_dir]:
        if not root:
            continue
        try:
            target.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise HTTPException(status_code=400, detail="path is outside the knowledge base directory")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    file_name = target.name
    vectors_deleted = get_chroma_service().delete_kb_source(file_name)
    target.unlink()
    return {"ok": True, "file": str(target), "vectors_deleted": vectors_deleted}


@app.delete("/api/admin/kb/vectors", dependencies=[Depends(require_admin)])
async def clear_kb_vectors():
    svc = get_chroma_service()
    if not svc.available:
        return {"ok": False, "detail": "chroma not available"}
    try:
        svc.client.delete_collection(svc.kb_name)
    except Exception:
        pass
    svc.kb_collection = svc.client.get_or_create_collection(name=svc.kb_name, metadata={"hnsw:space": "cosine"})
    return {"ok": True, "chroma": svc.stats()}


@app.delete("/api/admin/chroma/user-reports", dependencies=[Depends(require_admin)])
async def clear_user_report_vectors():
    deleted = get_chroma_service().delete_all_user_reports()
    return {"ok": True, "vectors_deleted": deleted, "chroma": get_chroma_service().stats()}


# 放在所有 /api 路由之后，避免静态文件挂载截获 API 请求。
FRONTEND_ROOT = PROJECT_ROOT / "admin_system" / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND_ROOT, html=True), name="admin_frontend")
