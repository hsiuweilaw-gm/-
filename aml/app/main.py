"""應用程式進入點。"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .db import SessionLocal, init_db
from .deps import current_user_optional
from .models import Role, User
from .routers import admin, api, assessments, auth, compliance, reports, review
from .security import hash_password

log = logging.getLogger("aml")

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
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
    if user.must_change_password:
        return RedirectResponse("/change-password", status_code=303)
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
