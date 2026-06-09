from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.database import clear_all, clear_session, list_messages, list_sessions
from app.core.database import session_belongs_to_user

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions")
async def sessions(scene: str | None = Query(default=None), user: dict = Depends(get_current_user)):
    return {"sessions": list_sessions(user_id=user["user_id"], scene=scene)}


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: dict = Depends(get_current_user)):
    if not session_belongs_to_user(session_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="session not found")
    return {"messages": list_messages(session_id, limit=100, with_attachments=True)}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    clear_session(session_id, user_id=user["user_id"])
    return {"ok": True}


@router.delete("/sessions")
async def delete_all_sessions(user: dict = Depends(get_current_user)):
    clear_all(user_id=user["user_id"])
    return {"ok": True}
