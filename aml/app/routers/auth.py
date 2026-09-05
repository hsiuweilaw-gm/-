"""登入、登出與變更密碼。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .. import ratelimit, totp
from ..config import get_settings
from ..db import get_db
from ..deps import (
    PENDING_COOKIE,
    SESSION_COOKIE,
    clear_failed_logins,
    current_user_any,
    is_locked,
    issue_pending,
    issue_session,
    pending_step,
    read_pending,
    register_failed_login,
)
from ..models import User, utcnow
from ..security import decrypt_pii, encrypt_pii, hash_password, verify_password
from ..services import audit
from ..templating import templates

router = APIRouter()
MIN_PASSWORD_LENGTH = 12


def client_ip(request: Request) -> str | None:
    """判定請求的真實來源位址。

    X-Forwarded-For 的左半段由客戶端自行填寫，取最左邊那一段等於讓對方
    自報來源——稽核軌跡的位址會被偽造，來源位址限流與白名單也一併失效。
    正確作法是從右邊往回數，數幾層由 trusted_proxy_hops 指定；
    設為 0 時完全忽略此標頭，一律以連線對端為準。
    """
    peer = request.client.host if request.client else None
    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    parts = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(parts) < hops:
        # 標頭比預期短，代表未經預期的代理鏈，寧可退回連線對端。
        return peer
    return parts[-hops]


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
    settings = get_settings()
    ip = client_ip(request) or "unknown"
    if ratelimit.too_many(ip, limit=settings.login_attempts_per_ip,
                          window=settings.login_attempt_window_seconds):
        audit.record(db, actor=None, action="auth.rate_limited", entity_type="user",
                     entity_id=username, ip=ip)
        db.commit()
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "同一位置嘗試次數過多，請稍後再試"}, status_code=429,
        )
    ratelimit.record(ip)

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

    if user.totp_confirmed_at is not None and user.totp_secret_enc:
        # 密碼只是第一道。核發效期五分鐘的中繼憑證，尚未登入。
        audit.record(db, actor=user, action="auth.password_ok", entity_type="user",
                     entity_id=user.username, ip=client_ip(request))
        db.commit()
        response = RedirectResponse("/login/verify", status_code=303)
        response.set_cookie(
            PENDING_COOKIE, issue_pending(user), httponly=True, samesite="lax",
            secure=get_settings().cookie_secure, max_age=300,
        )
        return response

    audit.record(db, actor=user, action="auth.login", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()
    ratelimit.clear(ip)
    return _establish_session(user)


def _establish_session(user: User) -> RedirectResponse:
    settings = get_settings()
    response = RedirectResponse(pending_step(user) or "/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, issue_session(user), httponly=True, samesite="lax",
        secure=settings.cookie_secure, max_age=settings.session_max_age_seconds,
    )
    response.delete_cookie(PENDING_COOKIE)
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user_any)):
    audit.record(db, actor=user, action="auth.logout", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request, user: User = Depends(current_user_any)):
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
    user: User = Depends(current_user_any),
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


@router.get("/login/verify", response_class=HTMLResponse)
def verify_form(request: Request):
    if read_pending(request.cookies.get(PENDING_COOKIE) or "") is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "totp_verify.html", {"error": None})


@router.post("/login/verify", response_class=HTMLResponse)
def verify_code(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    """第二道：一次性密碼。

    中繼憑證只證明密碼正確，不是登入狀態；驗證通過才核發工作階段。
    """
    settings = get_settings()
    ip = client_ip(request) or "unknown"
    pending = read_pending(request.cookies.get(PENDING_COOKIE) or "")
    if pending is None:
        return RedirectResponse("/login", status_code=303)

    if ratelimit.too_many(ip, limit=settings.login_attempts_per_ip,
                          window=settings.login_attempt_window_seconds):
        return templates.TemplateResponse(
            request, "totp_verify.html",
            {"error": "同一位置嘗試次數過多，請稍後再試"}, status_code=429,
        )
    ratelimit.record(ip)

    user = db.get(User, pending["uid"])
    if user is None or not user.active or not user.totp_secret_enc:
        return RedirectResponse("/login", status_code=303)
    if is_locked(user):
        return templates.TemplateResponse(
            request, "totp_verify.html",
            {"error": "帳號因多次失敗已暫時鎖定，請稍後再試或聯絡管理者"}, status_code=423,
        )

    counter = totp.verify(decrypt_pii(user.totp_secret_enc), code,
                          last_counter=user.totp_last_counter)
    if counter is None:
        # 一次性密碼錯誤與密碼錯誤共用同一組失敗計數：兩道都是登入嘗試。
        register_failed_login(db, user)
        audit.record(db, actor=None, action="auth.totp_failed", entity_type="user",
                     entity_id=user.username, ip=ip)
        db.commit()
        return templates.TemplateResponse(
            request, "totp_verify.html", {"error": "驗證碼不正確或已逾時"}, status_code=401,
        )

    user.totp_last_counter = counter
    clear_failed_logins(db, user)
    audit.record(db, actor=user, action="auth.login", entity_type="user",
                 entity_id=user.username, detail={"second_factor": "totp"}, ip=ip)
    db.commit()
    ratelimit.clear(ip)
    return _establish_session(user)


@router.get("/totp/setup", response_class=HTMLResponse)
def totp_setup_form(request: Request, db: Session = Depends(get_db),
                    user: User = Depends(current_user_any)):
    """設定雙因素驗證。

    密鑰一經產生即沿用到確認為止——每次重新整理都換一組的話，
    使用者掃過的 QR 就失效了。
    """
    if user.totp_confirmed_at is not None:
        return templates.TemplateResponse(
            request, "totp_setup.html",
            {"user": user, "already": True, "error": None}, status_code=200,
        )
    if not user.totp_secret_enc:
        user.totp_secret_enc = encrypt_pii(totp.generate_secret())
        user.totp_last_counter = None
        db.commit()

    secret = decrypt_pii(user.totp_secret_enc)
    uri = totp.provisioning_uri(secret, user.username, get_settings().company_name)
    return templates.TemplateResponse(
        request, "totp_setup.html",
        {
            "user": user, "already": False, "error": None,
            "secret_display": totp.format_secret(secret),
            "uri": uri,
            "qr_svg": totp.qr_svg(uri),
        },
    )


@router.post("/totp/setup", response_class=HTMLResponse)
def totp_setup_confirm(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(current_user_any),
):
    if user.totp_confirmed_at is not None or not user.totp_secret_enc:
        return RedirectResponse("/", status_code=303)

    secret = decrypt_pii(user.totp_secret_enc)
    counter = totp.verify(secret, code)
    if counter is None:
        uri = totp.provisioning_uri(secret, user.username, get_settings().company_name)
        return templates.TemplateResponse(
            request, "totp_setup.html",
            {
                "user": user, "already": False,
                "error": "驗證碼不正確。請確認手機時間為自動校時，並使用畫面上最新的六位數。",
                "secret_display": totp.format_secret(secret),
                "uri": uri,
                "qr_svg": totp.qr_svg(uri),
            },
            status_code=400,
        )

    user.totp_confirmed_at = utcnow()
    user.totp_last_counter = counter
    audit.record(db, actor=user, action="auth.totp_enabled", entity_type="user",
                 entity_id=user.username, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/", status_code=303)
