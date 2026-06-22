"""网页控制台鉴权核心：密码哈希、长效 JWT、当前用户依赖。

仅内网使用，从简：用户名密码登录，签发长效 token（默认 365 天）让用户本机
登一次即可。角色决定 Agent 应答风格，由
core.prompt_builder.ROLE_PROMPTS 消费。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Header, HTTPException

from core.database import SessionLocal
from core.models import UserModel
from settings import auth_config

# 角色 key -> (中文标签, 一句话说明)。/auth/roles 与注册下拉直接消费。
ROLES: dict[str, tuple[str, str]] = {
    "operations": ("运营", "少代码细节、口语化结论、严格统一业务术语、给可执行操作"),
    "product": ("产品", "关注业务影响、指标口径、需求拆分与验收标准"),
    "qa": ("测试", "关注复现路径、影响范围、验收标准与回归风险"),
    "developer": ("开发", "可给 file:line、堆栈、SQL、技术根因，最详尽"),
    "admin": ("管理员", "关注项目配置、知识治理、trace learning 与系统观测"),
}

_JWT_ALG = "HS256"


def role_label(role: str) -> str:
    entry = ROLES.get(role)
    return entry[0] if entry else role


def is_valid_role(role: str) -> bool:
    return role in ROLES


# ---------------------------------------------------------------------------
# 密码哈希（bcrypt）
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT 长效 token
# ---------------------------------------------------------------------------
def _secret() -> str:
    if not auth_config.jwt_secret:
        # 内网兜底：未配置 secret 时拒绝签发/校验，避免空 secret 造成可伪造 token。
        raise HTTPException(status_code=503, detail="服务未配置 VIKTOR_AUTH_SECRET，无法鉴权")
    return auth_config.jwt_secret


def create_token(user: UserModel) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "mobile": user.mobile or "",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=auth_config.token_ttl_days)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALG)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, _secret(), algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        return None


# ---------------------------------------------------------------------------
# 当前用户（FastAPI 依赖）
# ---------------------------------------------------------------------------
@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    role_label: str
    display_name: str
    mobile: str
    password_set: bool
    profile_key: str = ""
    primary_department: str = ""
    department_paths: list[str] | None = None


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少凭证")
    return authorization[7:].strip()


def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """解析 Bearer token → 从 DB 取最新用户（拿到最新 role / is_active）。"""
    token = _extract_bearer(authorization)
    claims = decode_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="凭证无效或已过期，请重新登录")
    db = SessionLocal()
    try:
        user = db.query(UserModel).filter(UserModel.id == int(claims.get("sub", 0))).first()
    finally:
        db.close()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已停用")
    return CurrentUser(
        id=user.id,
        username=user.username,
        role=user.role,
        role_label=role_label(user.role),
        display_name=user.display_name or user.username,
        mobile=user.mobile or "",
        password_set=bool(getattr(user, "password_set", 1)),
        profile_key=getattr(user, "profile_key", "") or user.role,
        primary_department=getattr(user, "primary_department", "") or "",
        department_paths=getattr(user, "department_paths", None) or [],
    )


# router 级依赖：只校验已登录，忽略返回值。
require_auth = get_current_user
