from __future__ import annotations

import json
import uuid
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from app.core.config import SETTINGS


DB_CONFIG = SETTINGS.get("database", {})
DB_TYPE = str(DB_CONFIG.get("type", "sqlite")).lower()
USE_POSTGRES = DB_TYPE in {"postgres", "postgresql"}
DB_PATH = Path(DB_CONFIG.get("sqlite_path", "../data/runtime/medix_enterprise.db"))
POSTGRES_URL = str(DB_CONFIG.get("postgres_url", ""))


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _convert_placeholders(sql: str) -> str:
    return sql.replace("?", "%s") if USE_POSTGRES else sql


def _ph(count: int) -> str:
    token = "%s" if USE_POSTGRES else "?"
    return ",".join(token for _ in range(count))


def get_conn():
    if USE_POSTGRES:
        if not POSTGRES_URL:
            raise RuntimeError("database.postgres_url is empty")
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(POSTGRES_URL, row_factory=dict_row, connect_timeout=5)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _execute(conn, sql: str, params: tuple | list = ()):
    return conn.execute(_convert_placeholders(sql), params)


def _row_to_dict(row: Any) -> Dict[str, Any]:
    return dict(row) if row is not None else {}


def init_db() -> None:
    with get_conn() as conn:
        if USE_POSTGRES:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    phone TEXT,
                    id_number TEXT,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    created_at TEXT,
                    last_login_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    scene TEXT,
                    title TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    metadata TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS encounters (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT,
                    user_id TEXT,
                    scene TEXT,
                    chief_complaint TEXT,
                    risk_level TEXT,
                    department TEXT,
                    summary TEXT,
                    metadata TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id BIGSERIAL PRIMARY KEY,
                    user_id TEXT,
                    department TEXT,
                    doctor TEXT,
                    doctor_title TEXT,
                    visit_date TEXT,
                    period TEXT,
                    time_slot TEXT,
                    status TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_attachments (
                    id BIGSERIAL PRIMARY KEY,
                    message_id BIGINT NOT NULL,
                    doc_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    position INTEGER DEFAULT 0,
                    created_at TEXT
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    phone TEXT,
                    id_number TEXT,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    created_at TEXT,
                    last_login_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    scene TEXT,
                    title TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    metadata TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS encounters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    user_id TEXT,
                    scene TEXT,
                    chief_complaint TEXT,
                    risk_level TEXT,
                    department TEXT,
                    summary TEXT,
                    metadata TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    department TEXT,
                    doctor TEXT,
                    doctor_title TEXT,
                    visit_date TEXT,
                    period TEXT,
                    time_slot TEXT,
                    status TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    doc_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    position INTEGER DEFAULT 0,
                    created_at TEXT,
                    FOREIGN KEY (message_id) REFERENCES messages(id),
                    FOREIGN KEY (doc_id) REFERENCES medical_documents(doc_id)
                )
                """
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_profiles (
                user_id TEXT PRIMARY KEY,
                age INTEGER,
                gender TEXT,
                chronic_diseases TEXT,
                allergy_history TEXT,
                medication_history TEXT,
                address TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS medical_documents (
                doc_id TEXT PRIMARY KEY,
                user_id TEXT,
                session_id TEXT,
                file_name TEXT,
                file_path TEXT,
                doc_type TEXT,
                title TEXT,
                summary TEXT,
                parsed_json TEXT,
                confidence REAL,
                page_count INTEGER,
                created_at TEXT
            )
            """
        )
        conn.commit()
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN scene TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
        for sql in [
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE users ADD COLUMN id_number TEXT",
            "ALTER TABLE health_profiles ADD COLUMN address TEXT",
        ]:
            try:
                conn.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id_number ON users(id_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_att_msg ON message_attachments(message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_att_user ON message_attachments(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_user ON medical_documents(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        conn.commit()


def create_user(
    username: str,
    password_hash: str,
    display_name: str = "",
    phone: str = "",
    id_number: str = "",
) -> Dict[str, Any]:
    user_id = uuid.uuid4().hex
    ts = now_text()
    with get_conn() as conn:
        _execute(
            conn,
            """
            INSERT INTO users(user_id, username, phone, id_number, password_hash, display_name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, phone or username, id_number, password_hash, display_name or username, ts, ts),
        )
        conn.commit()
    return {
        "user_id": user_id,
        "username": username,
        "phone": phone or username,
        "id_number": id_number,
        "display_name": display_name or username,
    }


def get_user_by_username(username: str) -> Dict[str, Any] | None:
    with get_conn() as conn:
        row = _execute(
            conn,
            """
            SELECT u.*, h.age, h.gender, h.chronic_diseases, h.allergy_history,
                   h.medication_history, h.address
            FROM users u
            LEFT JOIN health_profiles h ON h.user_id = u.user_id
            WHERE u.username=? OR u.phone=?
            """,
            (username, username),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_id(user_id: str) -> Dict[str, Any] | None:
    with get_conn() as conn:
        row = _execute(
            conn,
            """
            SELECT u.*, h.age, h.gender, h.chronic_diseases, h.allergy_history,
                   h.medication_history, h.address
            FROM users u
            LEFT JOIN health_profiles h ON h.user_id = u.user_id
            WHERE u.user_id=?
            """,
            (user_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def update_user_profile(
    user_id: str,
    *,
    phone: str | None = None,
    display_name: str | None = None,
    password_hash: str | None = None,
    age: int | None = None,
    gender: str | None = None,
    address: str | None = None,
    chronic_diseases: str | None = None,
    allergy_history: str | None = None,
    medication_history: str | None = None,
) -> Dict[str, Any]:
    with get_conn() as conn:
        fields: List[str] = []
        params: List[Any] = []
        if phone is not None:
            fields.extend(["username=?", "phone=?"])
            params.extend([phone, phone])
        if display_name is not None:
            fields.append("display_name=?")
            params.append(display_name)
        if password_hash is not None:
            fields.append("password_hash=?")
            params.append(password_hash)
        if fields:
            params.append(user_id)
            _execute(conn, f"UPDATE users SET {', '.join(fields)} WHERE user_id=?", params)
        _execute(
            conn,
            """
            INSERT INTO health_profiles(
                user_id, age, gender, chronic_diseases, allergy_history,
                medication_history, address, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                age=COALESCE(excluded.age, health_profiles.age),
                gender=COALESCE(excluded.gender, health_profiles.gender),
                chronic_diseases=COALESCE(excluded.chronic_diseases, health_profiles.chronic_diseases),
                allergy_history=COALESCE(excluded.allergy_history, health_profiles.allergy_history),
                medication_history=COALESCE(excluded.medication_history, health_profiles.medication_history),
                address=COALESCE(excluded.address, health_profiles.address),
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                age,
                gender,
                chronic_diseases,
                allergy_history,
                medication_history,
                address,
                now_text(),
            ),
        )
        conn.commit()
    return get_user_by_id(user_id) or {}


def touch_user_login(user_id: str) -> None:
    with get_conn() as conn:
        _execute(conn, "UPDATE users SET last_login_at=? WHERE user_id=?", (now_text(), user_id))
        conn.commit()


def upsert_session(session_id: str, title: str = "医疗问诊会话", user_id: str | None = None, scene: str | None = None) -> None:
    ts = now_text()
    with get_conn() as conn:
        _execute(
            conn,
            """
            INSERT INTO sessions(id, user_id, scene, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_id=COALESCE(excluded.user_id, sessions.user_id),
                scene=COALESCE(excluded.scene, sessions.scene),
                title=COALESCE(NULLIF(excluded.title, ''), sessions.title),
                updated_at=excluded.updated_at
            """,
            (session_id, user_id, scene, title, ts, ts),
        )
        conn.commit()


def add_message(session_id: str, role: str, content: str, metadata: Dict[str, Any] | None = None) -> int:
    with get_conn() as conn:
        params = (session_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now_text())
        if USE_POSTGRES:
            cur = conn.execute(
                """
                INSERT INTO messages(session_id, role, content, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                params,
            )
            message_id = int(cur.fetchone()["id"])
        else:
            cur = _execute(
                conn,
                "INSERT INTO messages(session_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                params,
            )
            message_id = int(cur.lastrowid)
        conn.commit()
        return message_id


def attach_documents_to_message(
    message_id: int,
    session_id: str | None,
    user_id: str,
    doc_ids: List[str],
) -> None:
    if not doc_ids:
        return
    ts = now_text()
    with get_conn() as conn:
        for position, doc_id in enumerate(doc_ids):
            row = _execute(conn, "SELECT user_id FROM medical_documents WHERE doc_id=?", (doc_id,)).fetchone()
            if not row or row["user_id"] != user_id:
                continue
            _execute(
                conn,
                """
                INSERT INTO message_attachments
                    (message_id, doc_id, user_id, session_id, position, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (message_id, doc_id, user_id, session_id, position, ts),
            )
        conn.commit()


def list_attachments_for_messages(message_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not message_ids:
        return {}
    placeholders = _ph(len(message_ids))
    sql = f"""
        SELECT a.message_id, a.doc_id, a.position, a.created_at,
               d.file_name, d.doc_type, d.title, d.summary,
               d.confidence, d.page_count
        FROM message_attachments a
        LEFT JOIN medical_documents d ON a.doc_id = d.doc_id
        WHERE a.message_id IN ({placeholders})
        ORDER BY a.message_id ASC, a.position ASC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, message_ids).fetchall() if USE_POSTGRES else conn.execute(sql, message_ids).fetchall()
    out: Dict[int, List[Dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(int(row["message_id"]), []).append(
            {
                "doc_id": row["doc_id"],
                "position": row["position"],
                "file_name": row["file_name"],
                "doc_type": row["doc_type"],
                "title": row["title"],
                "summary": row["summary"],
                "confidence": row["confidence"],
                "page_count": row["page_count"],
                "raw_url": f"/api/upload/medical-document/{row['doc_id']}/raw",
            }
        )
    return out


def get_documents_by_ids(doc_ids: List[str], user_id: str) -> List[Dict[str, Any]]:
    if not doc_ids:
        return []
    placeholders = _ph(len(doc_ids))
    sql = f"SELECT * FROM medical_documents WHERE doc_id IN ({placeholders}) AND user_id={('%s' if USE_POSTGRES else '?')}"
    params = list(doc_ids) + [user_id]
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    by_id = {row["doc_id"]: row for row in rows}
    result: List[Dict[str, Any]] = []
    for doc_id in doc_ids:
        row = by_id.get(doc_id)
        if not row:
            continue
        item = _row_to_dict(row)
        item["parsed_json"] = json.loads(item.get("parsed_json") or "{}")
        result.append(item)
    return result


def list_session_medical_documents(
    session_id: str,
    user_id: str,
    limit: int = 8,
    exclude_doc_ids: List[str] | None = None,
) -> List[Dict[str, Any]]:
    """按本会话附件关系取回用户报告，最近上传/引用的排前面。"""
    exclude_doc_ids = exclude_doc_ids or []
    params: List[Any] = [session_id, user_id]
    exclude_sql = ""
    if exclude_doc_ids:
        exclude_sql = f" AND d.doc_id NOT IN ({_ph(len(exclude_doc_ids))})"
        params.extend(exclude_doc_ids)
    params.append(limit)
    sql = f"""
        SELECT d.*, MAX(a.created_at) AS attached_at
        FROM message_attachments a
        JOIN medical_documents d ON d.doc_id = a.doc_id
        WHERE a.session_id=? AND a.user_id=? {exclude_sql}
        GROUP BY d.doc_id
        ORDER BY COALESCE(MAX(a.created_at), d.created_at) DESC
        LIMIT ?
    """
    with get_conn() as conn:
        rows = _execute(conn, sql, params).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row)
        item["parsed_json"] = json.loads(item.get("parsed_json") or "{}")
        result.append(item)
    return result


def list_messages(session_id: str, limit: int = 20, with_attachments: bool = False) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = _execute(
            conn,
            "SELECT id, role, content, metadata, created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    messages = [
        {
            "id": int(row["id"]),
            "role": row["role"],
            "content": row["content"],
            "metadata": json.loads(row["metadata"] or "{}"),
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]
    if with_attachments and messages:
        att_map = list_attachments_for_messages([m["id"] for m in messages])
        for msg in messages:
            msg["attachments"] = att_map.get(msg["id"], [])
    return messages


def list_sessions(limit: int = 50, user_id: str | None = None, scene: str | None = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if user_id and scene:
            rows = _execute(
                conn,
                "SELECT id, user_id, scene, title, created_at, updated_at FROM sessions WHERE user_id=? AND scene=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, scene, limit),
            ).fetchall()
        elif user_id:
            rows = _execute(
                conn,
                "SELECT id, user_id, scene, title, created_at, updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = _execute(
                conn,
                "SELECT id, user_id, scene, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def session_belongs_to_user(session_id: str, user_id: str) -> bool:
    with get_conn() as conn:
        row = _execute(conn, "SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    return bool(row and row.get("user_id") == user_id)


def add_encounter(
    session_id: str,
    user_id: str,
    scene: str,
    chief_complaint: str,
    risk_level: str,
    department: str,
    summary: str,
    metadata: Dict[str, Any] | None = None,
) -> None:
    with get_conn() as conn:
        _execute(
            conn,
            """
            INSERT INTO encounters(session_id, user_id, scene, chief_complaint, risk_level, department, summary, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                scene,
                chief_complaint,
                risk_level,
                department,
                summary,
                json.dumps(metadata or {}, ensure_ascii=False),
                now_text(),
            ),
        )
        conn.commit()


def list_encounters(user_id: str = "demo_user", days: int = 7) -> List[Dict[str, Any]]:
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        rows = _execute(
            conn,
            """
            SELECT * FROM encounters
            WHERE user_id=? AND created_at>=?
            ORDER BY created_at DESC
            """,
            (user_id, since),
        ).fetchall()
    result = []
    for row in rows:
        item = _row_to_dict(row)
        item["metadata"] = json.loads(item.get("metadata") or "{}")
        result.append(item)
    return result


def add_appointment(payload: Dict[str, Any]) -> int:
    with get_conn() as conn:
        existing = _execute(
            conn,
            """
            SELECT id FROM appointments
            WHERE user_id=? AND department=? AND doctor=? AND visit_date=? AND period=? AND time_slot=? AND status='已预约'
            LIMIT 1
            """,
            (
                payload.get("user_id", "demo_user"),
                payload["department"],
                payload["doctor"],
                payload["visit_date"],
                payload["period"],
                payload["time_slot"],
            ),
        ).fetchone()
        if existing:
            return int(existing["id"])
        if USE_POSTGRES:
            cur = conn.execute(
                """
                INSERT INTO appointments(user_id, department, doctor, doctor_title, visit_date, period, time_slot, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    payload.get("user_id", "demo_user"),
                    payload["department"],
                    payload["doctor"],
                    payload.get("doctor_title", ""),
                    payload["visit_date"],
                    payload["period"],
                    payload["time_slot"],
                    "已预约",
                    now_text(),
                ),
            )
            appointment_id = int(cur.fetchone()["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO appointments(user_id, department, doctor, doctor_title, visit_date, period, time_slot, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("user_id", "demo_user"),
                    payload["department"],
                    payload["doctor"],
                    payload.get("doctor_title", ""),
                    payload["visit_date"],
                    payload["period"],
                    payload["time_slot"],
                    "已预约",
                    now_text(),
                ),
            )
            appointment_id = int(cur.lastrowid)
        conn.commit()
        return appointment_id


def list_appointments(user_id: str = "demo_user") -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = _execute(
            conn,
            """
            SELECT * FROM appointments
            WHERE user_id=?
            ORDER BY visit_date ASC, period ASC, time_slot ASC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def cancel_appointment(appointment_id: int, user_id: str = "demo_user") -> bool:
    with get_conn() as conn:
        cur = _execute(
            conn,
            """
            UPDATE appointments
            SET status='已取消'
            WHERE id=? AND user_id=? AND status='已预约'
            """,
            (appointment_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def appointment_counts(user_id: str = "demo_user") -> Dict[str, int]:
    with get_conn() as conn:
        rows = _execute(
            conn,
            """
            SELECT department, doctor, visit_date, period, time_slot, COUNT(*) AS total
            FROM appointments
            WHERE user_id=? AND status='已预约'
            GROUP BY department, doctor, visit_date, period, time_slot
            """,
            (user_id,),
        ).fetchall()
    counts: Dict[str, int] = {}
    for row in rows:
        key = appointment_key(row["department"], row["doctor"], row["visit_date"], row["period"], row["time_slot"])
        counts[key] = int(row["total"])
    return counts


def appointment_key(department: str, doctor: str, visit_date: str, period: str, time_slot: str) -> str:
    return f"{department}|{doctor}|{visit_date}|{period}|{time_slot}"


def add_medical_document(
    doc_id: str,
    user_id: str,
    session_id: str | None,
    file_name: str,
    file_path: str,
    doc_type: str,
    title: str,
    summary: str,
    parsed: Dict[str, Any],
    confidence: float,
    page_count: int,
) -> None:
    with get_conn() as conn:
        _execute(
            conn,
            """
            INSERT INTO medical_documents
                (doc_id, user_id, session_id, file_name, file_path, doc_type,
                 title, summary, parsed_json, confidence, page_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                user_id=excluded.user_id,
                session_id=excluded.session_id,
                file_name=excluded.file_name,
                file_path=excluded.file_path,
                doc_type=excluded.doc_type,
                title=excluded.title,
                summary=excluded.summary,
                parsed_json=excluded.parsed_json,
                confidence=excluded.confidence,
                page_count=excluded.page_count,
                created_at=excluded.created_at
            """,
            (
                doc_id,
                user_id,
                session_id,
                file_name,
                file_path,
                doc_type,
                title,
                summary,
                json.dumps(parsed, ensure_ascii=False),
                float(confidence or 0.0),
                int(page_count or 0),
                now_text(),
            ),
        )
        conn.commit()


def list_medical_documents(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = _execute(
            conn,
            """
            SELECT doc_id, user_id, session_id, file_name, doc_type, title, summary,
                   parsed_json, confidence, page_count, created_at
            FROM medical_documents
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    result = []
    for row in rows:
        item = _row_to_dict(row)
        parsed = json.loads(item.pop("parsed_json") or "{}")
        item["parse_status"] = parsed.get("parse_status") or "ok"
        item["items"] = parsed.get("items") or []
        item["key_abnormalities"] = parsed.get("key_abnormalities") or []
        item["findings"] = parsed.get("findings") or ""
        item["impression"] = parsed.get("impression") or ""
        item["duration_ms"] = (parsed.get("processing") or {}).get("duration_ms")
        result.append(item)
    return result


def get_medical_document(doc_id: str) -> Dict[str, Any] | None:
    with get_conn() as conn:
        row = _execute(conn, "SELECT * FROM medical_documents WHERE doc_id=?", (doc_id,)).fetchone()
    if not row:
        return None
    item = _row_to_dict(row)
    item["parsed_json"] = json.loads(item.get("parsed_json") or "{}")
    return item


def list_all_medical_documents() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = _execute(conn, "SELECT * FROM medical_documents ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(row) for row in rows]


def delete_medical_document(doc_id: str, user_id: str) -> bool:
    with get_conn() as conn:
        cur = _execute(
            conn,
            "DELETE FROM medical_documents WHERE doc_id=? AND user_id=?",
            (doc_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_report_sessions_for_user(user_id: str) -> int:
    """删除当前用户上传报告参与过的会话，避免旧回答继续污染医疗记忆。"""
    with get_conn() as conn:
        rows = _execute(
            conn,
            "SELECT DISTINCT session_id FROM message_attachments WHERE user_id=? AND session_id IS NOT NULL",
            (user_id,),
        ).fetchall()
        session_ids = [row["session_id"] for row in rows if row["session_id"]]
        if session_ids:
            placeholders = _ph(len(session_ids))
            params = list(session_ids)
            conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM encounters WHERE session_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", params)
        conn.commit()
        return len(session_ids)


def delete_all_report_sessions() -> int:
    """删除所有带报告附件的会话；用于后台全量清理报告记忆。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM message_attachments WHERE session_id IS NOT NULL"
        ).fetchall()
        session_ids = [row["session_id"] for row in rows if row["session_id"]]
        if session_ids:
            placeholders = _ph(len(session_ids))
            params = list(session_ids)
            conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM encounters WHERE session_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM sessions WHERE id IN ({placeholders})", params)
        conn.commit()
        return len(session_ids)


def delete_medical_documents_for_user(user_id: str) -> int:
    with get_conn() as conn:
        _execute(conn, "DELETE FROM message_attachments WHERE user_id=?", (user_id,))
        cur = _execute(conn, "DELETE FROM medical_documents WHERE user_id=?", (user_id,))
        conn.commit()
        return cur.rowcount


def delete_all_medical_documents() -> int:
    with get_conn() as conn:
        conn.execute("DELETE FROM message_attachments")
        cur = conn.execute("DELETE FROM medical_documents")
        conn.commit()
        return cur.rowcount


def clear_session(session_id: str, user_id: str | None = None) -> None:
    with get_conn() as conn:
        if user_id:
            row = _execute(conn, "SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row or row.get("user_id") != user_id:
                return
        _execute(conn, "DELETE FROM messages WHERE session_id=?", (session_id,))
        _execute(conn, "DELETE FROM sessions WHERE id=?", (session_id,))
        conn.commit()


def clear_all(user_id: str | None = None) -> None:
    with get_conn() as conn:
        if user_id:
            _execute(conn, "DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)", (user_id,))
            _execute(conn, "DELETE FROM sessions WHERE user_id=?", (user_id,))
            _execute(conn, "DELETE FROM encounters WHERE user_id=?", (user_id,))
            _execute(conn, "DELETE FROM appointments WHERE user_id=?", (user_id,))
        else:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM encounters")
            conn.execute("DELETE FROM appointments")
        conn.commit()
