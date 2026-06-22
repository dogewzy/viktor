"""网页控制台登录鉴权接口（公开，无需登录即可访问 register/login/roles）。

内网用户名密码登录，签发长效 JWT；支持自助注册（选角色）与快速改密。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_

from core.auth import (
    CurrentUser,
    ROLES,
    create_token,
    get_current_user,
    hash_password,
    is_valid_role,
    role_label,
    verify_password,
)
from core.database import SessionLocal
from core.models import UserModel
from settings import auth_config

router = APIRouter(prefix="/api/v1/auth", tags=["登录鉴权"])


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(..., description="operations / product / qa / developer / admin")
    display_name: str = Field(default="", max_length=64)
    mobile: str = Field(..., min_length=5, max_length=32, description="钉钉手机号，用于关联待办和通知")


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class DingtalkLoginRequest(BaseModel):
    mobile: str = Field(..., min_length=5, max_length=32)
    real_name: str = Field(..., min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)


class SetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=128)


def _user_public(user: UserModel) -> dict:
    return {
        "username": user.username,
        "role": user.role,
        "role_label": role_label(user.role),
        "display_name": user.display_name or user.username,
        "mobile": user.mobile or "",
        "password_set": bool(getattr(user, "password_set", 1)),
        "auth_source": getattr(user, "auth_source", "") or "local",
        "profile_key": getattr(user, "profile_key", "") or user.role,
        "primary_department": getattr(user, "primary_department", "") or "",
        "department_paths": getattr(user, "department_paths", None) or [],
    }


def _directory_user(user: UserModel) -> dict:
    return {
        "username": user.username,
        "display_name": user.display_name or user.username,
        "mobile": user.mobile or "",
        "dingtalk_userid": getattr(user, "dingtalk_userid", "") or "",
        "primary_department": getattr(user, "primary_department", "") or "",
        "department_paths": getattr(user, "department_paths", None) or [],
        "profile_key": getattr(user, "profile_key", "") or user.role,
        "auth_source": getattr(user, "auth_source", "") or "local",
    }


def _valid_roles_text() -> str:
    return " / ".join(ROLES.keys())


def _normalize_mobile(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("+"):
        return "+" + "".join(ch for ch in raw[1:] if ch.isdigit())
    return "".join(ch for ch in raw if ch.isdigit())


def _normalize_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


@router.get("/roles", summary="可选角色列表（注册下拉）")
def list_roles() -> dict:
    return {
        "roles": [
            {"key": key, "label": label, "description": desc}
            for key, (label, desc) in ROLES.items()
        ]
    }


@router.post("/register", summary="自助注册")
def register(body: RegisterRequest) -> dict:
    if not auth_config.allow_registration:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员")
    username = body.username.strip()
    mobile = _normalize_mobile(body.mobile)
    if not is_valid_role(body.role):
        raise HTTPException(status_code=400, detail=f"role 必须是 {_valid_roles_text()} 之一")
    if not mobile:
        raise HTTPException(status_code=400, detail="手机号必填")
    db = SessionLocal()
    try:
        if db.query(UserModel).filter(UserModel.username == username).first():
            raise HTTPException(status_code=409, detail="用户名已存在")
        if db.query(UserModel).filter(UserModel.mobile == mobile).first():
            raise HTTPException(status_code=409, detail="手机号已注册")
        user = UserModel(
            username=username,
            password_hash=hash_password(body.password),
            password_set=1,
            role=body.role,
            display_name=body.display_name.strip(),
            mobile=mobile,
            profile_key=body.role,
            auth_source="local",
            is_active=1,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return {"token": create_token(user), "user": _user_public(user)}
    finally:
        db.close()


@router.post("/login", summary="登录")
def login(body: LoginRequest) -> dict:
    identifier = body.username.strip()
    mobile = _normalize_mobile(identifier)
    db = SessionLocal()
    try:
        filters = [UserModel.username == identifier]
        if mobile:
            filters.append(UserModel.mobile == mobile)
        user = (
            db.query(UserModel)
            .filter(or_(*filters))
            .first()
        )
        if user is not None and not bool(getattr(user, "password_set", 1)):
            raise HTTPException(status_code=403, detail="账号已导入钉钉通讯录，请先使用手机号和真名完成首次激活")
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(status_code=401, detail="手机号或密码错误")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="用户已停用")
        return {"token": create_token(user), "user": _user_public(user)}
    finally:
        db.close()


@router.post("/dingtalk-login", summary="钉钉通讯录首次激活登录")
def dingtalk_login(body: DingtalkLoginRequest) -> dict:
    mobile = _normalize_mobile(body.mobile)
    real_name = _normalize_name(body.real_name)
    if not mobile:
        raise HTTPException(status_code=400, detail="手机号必填")
    if not real_name:
        raise HTTPException(status_code=400, detail="真名必填")
    db = SessionLocal()
    try:
        user = db.query(UserModel).filter(UserModel.mobile == mobile).first()
        if user is None:
            raise HTTPException(status_code=404, detail="未在钉钉通讯录中找到该手机号")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="用户已停用")
        if bool(getattr(user, "password_set", 1)):
            raise HTTPException(status_code=409, detail="账号已激活，请使用手机号和密码登录")
        expected = _normalize_name(user.display_name or user.username)
        if expected != real_name:
            raise HTTPException(status_code=401, detail="手机号和真名不匹配")
        return {"token": create_token(user), "user": _user_public(user)}
    finally:
        db.close()


@router.get("/me", summary="当前登录用户")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {
        "user": {
            "username": user.username,
            "role": user.role,
            "role_label": user.role_label,
            "display_name": user.display_name,
            "mobile": user.mobile,
            "password_set": user.password_set,
            "profile_key": user.profile_key,
            "primary_department": user.primary_department,
            "department_paths": user.department_paths or [],
        }
    }


@router.get("/users", summary="登录用户可用的钉钉通讯录")
def list_users(
    q: str = "",
    limit: int = 300,
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    keyword = q.strip()
    db = SessionLocal()
    try:
        query = db.query(UserModel).filter(UserModel.is_active == 1)
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(
                or_(
                    UserModel.username.like(like),
                    UserModel.display_name.like(like),
                    UserModel.mobile.like(like),
                    UserModel.primary_department.like(like),
                )
            )
        rows = (
            query.order_by(UserModel.primary_department.asc(), UserModel.display_name.asc(), UserModel.username.asc())
            .limit(max(1, min(limit, 1000)))
            .all()
        )
        return {"items": [_directory_user(user) for user in rows], "total": len(rows)}
    finally:
        db.close()


@router.post("/set-password", summary="首次激活后设置登录密码")
def set_password(
    body: SetPasswordRequest,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    db = SessionLocal()
    try:
        user = db.query(UserModel).filter(UserModel.id == current.id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if bool(getattr(user, "password_set", 1)):
            raise HTTPException(status_code=400, detail="密码已设置，请使用修改密码入口")
        user.password_hash = hash_password(body.new_password)
        user.password_set = 1
        db.commit()
        db.refresh(user)
        return {"ok": True, "token": create_token(user), "user": _user_public(user)}
    finally:
        db.close()


@router.post("/change-password", summary="修改密码")
def change_password(
    body: ChangePasswordRequest,
    current: CurrentUser = Depends(get_current_user),
) -> dict:
    db = SessionLocal()
    try:
        user = db.query(UserModel).filter(UserModel.id == current.id).first()
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if not bool(getattr(user, "password_set", 1)):
            raise HTTPException(status_code=400, detail="请先完成首次密码设置")
        if not verify_password(body.old_password, user.password_hash):
            raise HTTPException(status_code=400, detail="原密码错误")
        user.password_hash = hash_password(body.new_password)
        user.password_set = 1
        db.commit()
        return {"ok": True}
    finally:
        db.close()
