from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import SETTINGS
from app.core.database import get_user_by_id


_bearer = HTTPBearer(auto_error=False)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """PBKDF2 哈希，避免明文保存密码。"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_b64, digest_b64 = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _token_secret() -> bytes:
    secret = SETTINGS.get("auth", {}).get("token_secret") or "medical-agent-dev-secret"
    return str(secret).encode("utf-8")


def create_access_token(user: Dict[str, Any]) -> str:
    expire_hours = int(SETTINGS.get("auth", {}).get("token_expire_hours", 168))
    payload = {
        "sub": user["user_id"],
        "username": user["username"],
        "exp": int(time.time()) + expire_hours * 3600,
    }
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(body).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效") from exc


def public_user(user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "phone": user.get("phone") or user["username"],
        "id_number": user.get("id_number") or "",
        "display_name": user.get("display_name") or user["username"],
        "age": user.get("age"),
        "gender": user.get("gender") or "",
        "address": user.get("address") or "",
        "chronic_diseases": user.get("chronic_diseases") or "",
        "allergy_history": user.get("allergy_history") or "",
        "medication_history": user.get("medication_history") or "",
    }


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Dict[str, Any]:
    token = credentials.credentials if credentials else request.query_params.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    payload = decode_access_token(token)
    user = get_user_by_id(payload["sub"])
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user
