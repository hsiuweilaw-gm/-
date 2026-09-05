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
from .models import Assessment, Role, User, as_aware, utcnow

SESSION_COOKIE = "aml_session"
# 密碼已驗證、尚待一次性密碼的中繼憑證。刻意與正式工作階段分開，
# 且效期極短——它代表「通過了第一道」，不是登入狀態。
PENDING_COOKIE = "aml_pending_2fa"
PENDING_MAX_AGE = 5 * 60
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="aml-session")


def _pending_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="aml-pending-2fa")


def issue_session(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "u": user.username})


def read_session(token: str) -> dict | None:
    try:
        return _serializer().loads(token, max_age=get_settings().session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None


def issue_pending(user: User) -> str:
    return _pending_serializer().dumps({"uid": user.id})


def read_pending(token: str) -> dict | None:
    try:
        return _pending_serializer().loads(token, max_age=PENDING_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


class OnboardingRequired(Exception):
    """帳號尚有未完成的必辦事項（改密碼、設定雙因素），須先導向該頁。"""

    def __init__(self, target: str) -> None:
        self.target = target
        super().__init__(target)


def pending_step(user: User) -> str | None:
    """回傳此帳號還沒完成、且必須先完成的事項頁面。"""
    if user.must_change_password:
        return "/change-password"
    if get_settings().totp_required and user.totp_confirmed_at is None:
        return "/totp/setup"
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


def current_user_any(user: User | None = Depends(current_user_optional)) -> User:
    """已登入即可，不檢查必辦事項。僅供必辦事項本身的頁面使用。"""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "請先登入")
    return user


def current_user(user: User = Depends(current_user_any)) -> User:
    """已登入且必辦事項均已完成。

    必辦事項若只在首頁檢查，使用者直接輸入其他網址即可略過——
    這道檢查因此放在相依項，涵蓋每一個受保護的頁面。
    """
    step = pending_step(user)
    if step:
        raise OnboardingRequired(step)
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


def can_see_score(user: User) -> bool:
    """風險分數與等級是否對此角色揭露。

    公司政策：業務人員不得知悉客戶評分與風險等級，避免為規避高風險客戶之
    強化盡職調查而調整作答。業務員僅在跨越門檻時收到「須照會主管」之警示。
    """
    return user.role in (Role.SUPERVISOR, Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN)


def can_unmask_pii(user: User) -> bool:
    """明文個資僅第二、三道防線與案件承辦人可見；其餘一律遮罩。"""
    return user.role in (Role.COMPLIANCE, Role.AUDITOR, Role.ADMIN)


def register_failed_login(db: Session, user: User) -> None:
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILED_LOGINS:
        user.locked_until = utcnow() + LOCKOUT
    db.commit()


def is_locked(user: User) -> bool:
    locked_until = as_aware(user.locked_until)
    return bool(locked_until and locked_until > utcnow())


def clear_failed_logins(db: Session, user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.commit()
