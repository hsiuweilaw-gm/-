"""應用程式進入點。"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import SessionLocal, assert_schema_current
from .deps import (
    OnboardingRequired,
    PrivilegedAddressBlocked,
    current_user_optional,
    pending_step,
)
from .models import Role, User
from .routers import admin, api, assessments, auth, compliance, reports, review
from .security import hash_password
from .services import audit

log = logging.getLogger("aml")

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # 不自動建表：結構一律由 Alembic 遷移管理，此處只驗證是否為最新版本。
    assert_schema_current()
    bootstrap_admin()
    yield


app = FastAPI(
    title="洗錢防制客戶風險評估系統", docs_url=None, redoc_url=None, lifespan=lifespan
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)
app.include_router(assessments.router)
app.include_router(api.router)
app.include_router(review.router)
app.include_router(compliance.router)
app.include_router(reports.router)
app.include_router(admin.router)


@app.exception_handler(PrivilegedAddressBlocked)
async def privileged_address_blocked(request: Request, exc: PrivilegedAddressBlocked):
    """高權限角色自未經核准的位址存取：擋下並留痕。

    留痕本身就是價值——洗防或稽核的帳號從外部被使用，是憑證外洩的
    重要徵候，必須看得到。
    """
    with SessionLocal() as db:
        audit.record(db, actor=exc.user, action="auth.blocked_address", entity_type="user",
                     entity_id=exc.user.username, detail={"path": request.url.path}, ip=exc.ip)
        db.commit()
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "此帳號僅限自公司核准之位址使用"}, status_code=403)
    return HTMLResponse(
        "<h1>存取遭拒</h1><p>此帳號僅限自公司核准之位址使用。"
        "若您人在公司內仍看到本訊息，請聯絡資訊人員確認設定。</p>",
        status_code=403,
    )


@app.exception_handler(OnboardingRequired)
async def onboarding_redirect(request: Request, exc: OnboardingRequired):
    """帳號還有必辦事項（改密碼、設定雙因素）時，一律導回該頁。

    以相依項＋例外處理器實作，而不是只在首頁檢查——後者只要直接輸入
    其他網址就能略過。API 請求回傳 401，避免自動儲存的請求被導向到 HTML。
    """
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "請先完成帳號設定"}, status_code=401)
    return RedirectResponse(exc.target, status_code=303)


def bootstrap_admin() -> None:
    """首次啟動時依環境變數建立管理者帳號。未設定密碼則略過。"""
    settings = get_settings()
    if not settings.bootstrap_admin_password:
        return
    with SessionLocal() as db:
        exists = db.query(User).filter(User.username == settings.bootstrap_admin_username).first()
        if exists:
            return
        db.add(
            User(
                username=settings.bootstrap_admin_username,
                display_name="系統管理者",
                password_hash=hash_password(settings.bootstrap_admin_password),
                role=Role.ADMIN,
                must_change_password=True,
            )
        )
        db.commit()
        log.info("已建立初始管理者帳號：%s", settings.bootstrap_admin_username)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: User | None = Depends(current_user_optional)):
    if user is None:
        return RedirectResponse("/login", status_code=303)
    step = pending_step(user)
    if step:
        return RedirectResponse(step, status_code=303)
    destination = {
        Role.AGENT: "/assessments",
        Role.SUPERVISOR: "/review",
        Role.COMPLIANCE: "/compliance",
        Role.AUDITOR: "/compliance",
        Role.ADMIN: "/admin",
    }[user.role]
    return RedirectResponse(destination, status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "date": date.today().isoformat()}
