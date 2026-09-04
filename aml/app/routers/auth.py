"""登入、登出與變更密碼。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import (
    SESSION_COOKIE,
    clear_failed_logins,
    current_user,
    is_locked,
    issue_session,
    register_failed_login,
)
from ..models import User
from ..security import hash_password, verify_password
from ..services import audit
from ..templating import templates

router = APIRouter()
MIN_PASSWORD_LENGTH = 12


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username.strip()).one_or_none()
    # 帳號不存在與密碼錯誤回傳相同訊息，避免帳號列舉。
    generic = "帳號或密碼錯誤"

    if user is None or not user.active:
        audit.record(
            db, actor=None, action="auth.login_failed", entity_type="user",
            entity_id=username, detail={"reason": "unknown_or_inactive"}, ip=client_ip(request),
        )
        db.commit()
        return templates.TemplateResponse(
            request, "login.html", {"error": generic}, status_code=401
        )

    if is_locked(user):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "帳號因多次登入失敗已暫時鎖定，請稍後再試或聯絡管理者"}, status_code=423,
        )

    if not verify_password(password, user.password_hash):
        register_failed_login(db, user)
        audit.record(
            db, actor=None, action="auth.login_failed", entity_type="user",
            entity_id=user.username, detail={"reason": "bad_password"}, ip=client_ip(request),
        )
        db.commit()
        return templates.TemplateResponse(
            request, "login.html", {"error": generic}, status_code=401
        )

    clear_failed_logins(db, user)
    audit.record(db, actor=user, action="auth.login", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()

    target = "/change-password" if user.must_change_password else "/"
    response = RedirectResponse(target, status_code=303)
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE, issue_session(user), httponly=True, samesite="lax",
        secure=settings.cookie_secure, max_age=settings.session_max_age_seconds,
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    audit.record(db, actor=user, action="auth.logout", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request, "change_password.html",
        {"user": user, "error": None, "min_len": MIN_PASSWORD_LENGTH},
    )


@router.post("/change-password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    def fail(message: str):
        return templates.TemplateResponse(
            request, "change_password.html",
            {"user": user, "error": message, "min_len": MIN_PASSWORD_LENGTH}, status_code=400,
        )

    if not verify_password(current, user.password_hash):
        return fail("目前密碼不正確")
    if new_password != confirm:
        return fail("兩次輸入的新密碼不一致")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return fail(f"新密碼長度至少 {MIN_PASSWORD_LENGTH} 個字元")
    if verify_password(new_password, user.password_hash):
        return fail("新密碼不得與目前密碼相同")

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    audit.record(db, actor=user, action="auth.password_changed", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/", status_code=303)
