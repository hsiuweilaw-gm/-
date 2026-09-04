"""系統管理：帳號、組織單位。

保經業務員須經登錄始得招攬，且每年應受洗錢防制教育訓練（範本第十三點）。
本頁同時作為登錄有效期與訓練完訓狀態的勾稽介面。
"""
from __future__ import annotations

import io
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import OrgUnit, Role, User
from ..security import hash_password, new_token
from ..services import audit, roster
from ..templating import templates
from .auth import client_ip

router = APIRouter(prefix="/admin")


def _admin_context(db: Session, user: User, **extra) -> dict:
    users = db.query(User).order_by(User.role, User.username).all()
    units = db.query(OrgUnit).order_by(OrgUnit.code).all()
    return {
        "user": user, "users": users, "units": units, "roles": list(Role),
        "today": date.today(), "unit_names": {u.id: u.name for u in units},
    } | extra


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html", _admin_context(db, user))


@router.get("/roster/template.csv")
def roster_template(user: User = Depends(require_admin)):
    """名冊匯入範本。加上 BOM，Excel 開啟才不會把中文顯示成亂碼。"""
    data = ("\ufeff" + roster.template_csv()).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(data), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="roster-template.csv"'},
    )


@router.post("/roster", response_class=HTMLResponse)
async def import_roster(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """批次匯入業務員名冊。任一列有誤即整批不寫入。"""
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            # Excel 在中文 Windows 另存 CSV 預設為 Big5。
            content = raw.decode("cp950")
        except UnicodeDecodeError:
            message = "檔案編碼無法辨識，請另存為 UTF-8 或 Big5 的 CSV"
            return templates.TemplateResponse(
                request, "admin.html", _admin_context(db, user, roster_error=message),
                status_code=400,
            )

    result = roster.parse_and_import(db, content)
    if not result.ok:
        db.rollback()
        audit.record(db, actor=user, action="roster.import_failed", entity_type="user",
                     entity_id=None, detail={"errors": len(result.errors)}, ip=client_ip(request))
        db.commit()
        return templates.TemplateResponse(
            request, "admin.html", _admin_context(db, user, roster_result=result), status_code=400,
        )

    audit.record(
        db, actor=user, action="roster.import", entity_type="user", entity_id=None,
        detail={"created": [c.username for c in result.created], "updated": result.updated},
        ip=client_ip(request),
    )
    db.commit()
    return templates.TemplateResponse(
        request, "admin.html",
        _admin_context(db, user, roster_result=result,
                       credentials_csv=roster.credentials_csv(result.created)),
    )


@router.post("/org-units")
def create_unit(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    code = code.strip()
    if db.query(OrgUnit).filter(OrgUnit.code == code).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "單位代碼已存在")
    db.add(OrgUnit(code=code, name=name.strip()))
    audit.record(db, actor=user, action="org_unit.create", entity_type="org_unit",
                 entity_id=code, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/users")
def create_user(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    role: str = Form(...),
    org_unit_id: str = Form(""),
    agent_license_no: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    username = username.strip()
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "帳號已存在")
    try:
        role_value = Role(role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "無效的角色") from exc

    # 初始密碼由系統產生並僅顯示一次，強制首次登入變更。
    initial_password = new_token()[:16]
    created = User(
        username=username,
        display_name=display_name.strip(),
        password_hash=hash_password(initial_password),
        role=role_value,
        org_unit_id=int(org_unit_id) if org_unit_id.strip() else None,
        agent_license_no=agent_license_no.strip() or None,
        must_change_password=True,
    )
    db.add(created)
    audit.record(db, actor=user, action="user.create", entity_type="user",
                 entity_id=username, detail={"role": role_value.value}, ip=client_ip(request))
    db.commit()

    return templates.TemplateResponse(
        request, "admin.html",
        _admin_context(db, user,
                       new_credential={"username": username, "password": initial_password}),
    )


@router.post("/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "查無此帳號")
    if target.id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不可停用自己的帳號")
    target.active = not target.active
    audit.record(db, actor=user, action="user.toggle_active", entity_type="user",
                 entity_id=target.username, detail={"active": target.active},
                 ip=client_ip(request))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "查無此帳號")
    initial_password = new_token()[:16]
    target.password_hash = hash_password(initial_password)
    target.must_change_password = True
    target.failed_logins = 0
    target.locked_until = None
    audit.record(db, actor=user, action="user.reset_password", entity_type="user",
                 entity_id=target.username, ip=client_ip(request))
    db.commit()

    return templates.TemplateResponse(
        request, "admin.html",
        _admin_context(db, user,
                       new_credential={"username": target.username,
                                       "password": initial_password}),
    )


@router.post("/users/{user_id}/training")
def record_training(
    request: Request,
    user_id: int,
    training_date: date = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """登錄洗錢防制教育訓練完訓日（範本第十三點第五款）。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "查無此帳號")
    target.aml_training_date = training_date
    audit.record(db, actor=user, action="user.training", entity_type="user",
                 entity_id=target.username, detail={"date": training_date.isoformat()},
                 ip=client_ip(request))
    db.commit()
    return RedirectResponse("/admin", status_code=303)
