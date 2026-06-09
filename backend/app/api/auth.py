from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import create_access_token, get_current_user, hash_password, public_user, verify_password
from app.core.database import create_user, get_user_by_username, touch_user_login, update_user_profile


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginPayload(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11)
    password: str = Field(..., min_length=6, max_length=72)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        value = value.strip()
        if not value.isdigit() or len(value) != 11:
            raise ValueError("手机号必须是 11 位数字")
        return value


class RegisterPayload(LoginPayload):
    id_number: str = Field(..., min_length=15, max_length=18)
    display_name: str = Field(..., min_length=1, max_length=32)

    @field_validator("id_number")
    @classmethod
    def validate_id_number(cls, value: str) -> str:
        value = value.strip()
        if len(value) not in {15, 18}:
            raise ValueError("身份证号必须为 15 或 18 位")
        if len(value) == 18:
            head, tail = value[:-1], value[-1].upper()
            if not head.isdigit() or (not tail.isdigit() and tail != "X"):
                raise ValueError("身份证号格式不正确")
        elif not value.isdigit():
            raise ValueError("身份证号格式不正确")
        return value


class ProfileUpdatePayload(BaseModel):
    phone: str | None = Field(default=None, min_length=11, max_length=11)
    password: str | None = Field(default=None, min_length=6, max_length=72)
    gender: str | None = Field(default=None, max_length=8)
    age: int | None = Field(default=None, ge=0, le=130)
    address: str | None = Field(default=None, max_length=120)
    chronic_diseases: str | None = Field(default=None, max_length=500)
    allergy_history: str | None = Field(default=None, max_length=500)
    medication_history: str | None = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def validate_optional_phone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value.isdigit() or len(value) != 11:
            raise ValueError("手机号必须是 11 位数字")
        return value


def _auth_response(user: dict) -> dict:
    return {"token": create_access_token(user), "user": public_user(user)}


@router.post("/register")
async def register(payload: RegisterPayload):
    phone = payload.phone.strip()
    if get_user_by_username(phone):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手机号已注册")
    user = create_user(
        phone,
        hash_password(payload.password),
        payload.display_name.strip(),
        phone=phone,
        id_number=payload.id_number.strip().upper(),
    )
    return _auth_response(user)


@router.post("/login")
async def login(payload: LoginPayload):
    user = get_user_by_username(payload.phone.strip())
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="手机号或密码错误")
    touch_user_login(user["user_id"])
    user = get_user_by_username(payload.phone.strip()) or user
    return _auth_response(user)


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {"user": public_user(user)}


@router.patch("/me")
async def update_me(payload: ProfileUpdatePayload, user: dict = Depends(get_current_user)):
    phone = payload.phone.strip() if payload.phone else None
    if phone and phone != (user.get("phone") or user.get("username")):
        existed = get_user_by_username(phone)
        if existed and existed.get("user_id") != user["user_id"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="手机号已被其他用户使用")
    updated = update_user_profile(
        user["user_id"],
        phone=phone,
        password_hash=hash_password(payload.password) if payload.password else None,
        age=payload.age,
        gender=payload.gender.strip() if payload.gender is not None else None,
        address=payload.address.strip() if payload.address is not None else None,
        chronic_diseases=payload.chronic_diseases.strip() if payload.chronic_diseases is not None else None,
        allergy_history=payload.allergy_history.strip() if payload.allergy_history is not None else None,
        medication_history=payload.medication_history.strip() if payload.medication_history is not None else None,
    )
    return {"user": public_user(updated)}
