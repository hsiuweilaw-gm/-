"""登入狀態與角色權限。

權限模型對應內控三道防線，並額外遵守最小揭露原則：
業務員只看得到自己的案件，主管看得到所屬單位，洗防專責與稽核看得到全公司。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Assessment, Role, User, utcnow

SESSION_COOKIE = "aml_session"
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="aml-session")


def issue_session(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "u": user.username})


def read_session(token: str) -> dict | None:
    try:
        return _serializer().loads(token, max_age=get_settings().session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None


def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = read_session(token)
    if not data:
        return None
    user = db.get(User, data["uid"])
    if user is None or not user.active:
        return None
    return user


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "請先登入")
    return user


def require_roles(*roles: Role) -> Callable[[User], User]:
    allowed = set(roles)

    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "權限不足")
        return user

    return dependency


# 常用組合
require_compliance = require_roles(Role.COMPLIANCE, Role.ADMIN)
require_oversight = require_roles(Role.SUPERVISOR, Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN)
require_admin = require_roles(Role.ADMIN)


def can_view_assessment(user: User, assessment: Assessment) -> bool:
    if user.role in (Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN):
        return True
    if user.role == Role.SUPERVISOR:
        return assessment.org_unit_id == user.org_unit_id
    return assessment.agent_id == user.id


def can_edit_assessment(user: User, assessment: Assessment) -> bool:
    """僅承辦業務員本人、且案件仍為草稿時可編輯。送出後一律鎖定。"""
    from .models import AssessmentStatus

    return assessment.agent_id == user.id and assessment.status == AssessmentStatus.DRAFT


def can_unmask_pii(user: User) -> bool:
    """明文個資僅第二、三道防線與案件承辦人可見；其餘一律遮罩。"""
    return user.role in (Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN)


def register_failed_login(db: Session, user: User) -> None:
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED_LOGINS:
        user.locked_until = utcnow() + LOCKOUT
    db.commit()


def is_locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > utcnow())


def clear_failed_logins(db: Session, user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.commit()
