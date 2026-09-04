"""洗防專責後台：即時監控、案件查詢、STR 標記、名單維護、行為異常檢視。"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_compliance, require_oversight
from ..models import (
    Assessment,
    AssessmentStatus,
    OrgUnit,
    RiskLevel,
    Role,
    User,
    WatchListEntry,
    utcnow,
)
from ..security import decrypt_pii, mask_id_number, mask_name
from ..services import aggregate, anomalies, audit, screening
from ..templating import templates
from .assessments import case_context, load_case
from .auth import client_ip

router = APIRouter(prefix="/compliance")


def _org_names(db: Session) -> dict[int, str]:
    return {o.id: o.name for o in db.query(OrgUnit).all()}


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_oversight)):
    today = date.today()
    month_start = today.replace(day=1)
    summary = aggregate.summarize(db, month_start, today)

    queue = (
        db.query(Assessment)
        .filter(Assessment.status.in_(
            (AssessmentStatus.PENDING_APPROVAL, AssessmentStatus.BLOCKED)
        ))
        .order_by(Assessment.submitted_at.asc())
        .limit(50)
        .all()
    )
    str_pending = (
        db.query(Assessment)
        .filter(Assessment.checks_json.like('%"suspicious"%'),
                Assessment.str_reported.is_(False),
                Assessment.status != AssessmentStatus.DRAFT)
        .order_by(Assessment.submitted_at.asc())
        .limit(50)
        .all()
    )
    # 只掃描近 90 天，避免每次開啟儀表板都全表掃描。
    signals = anomalies.scan(db, since=utcnow() - timedelta(days=90), limit=200)

    return templates.TemplateResponse(
        request, "compliance_dashboard.html",
        {
            "user": user,
            "summary": summary,
            "queue": queue,
            "str_pending": str_pending,
            "signals": signals[:20],
            "signal_total": len(signals),
            "org_names": _org_names(db),
            "risk_band": aggregate.risk_band,
        },
    )


@router.get("/cases", response_class=HTMLResponse)
def cases(
    request: Request,
    q: str = Query("", max_length=64),
    level: str = Query(""),
    case_status: str = Query("", alias="status"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_oversight),
):
    query = db.query(Assessment)
    if level in ("high", "general"):
        query = query.filter(
            Assessment.risk_level == (RiskLevel.HIGH if level == "high" else RiskLevel.GENERAL)
        )
    if case_status:
        try:
            query = query.filter(Assessment.status == AssessmentStatus(case_status))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "無效的狀態") from exc
    if start:
        query = query.filter(
            Assessment.submitted_at >= datetime.combine(start, time.min, tzinfo=UTC)
        )
    if end:
        query = query.filter(
            Assessment.submitted_at <= datetime.combine(end, time.max, tzinfo=UTC)
        )
    if q.strip():
        # 案號直接比對；姓名／身分證字號因加密儲存，改以案號或業務員姓名查詢。
        term = f"%{q.strip()}%"
        query = query.join(User, Assessment.agent_id == User.id).filter(
            (Assessment.case_no.ilike(term))
            | (Assessment.policy_no.ilike(term))
            | (User.display_name.ilike(term))
        )

    rows = query.order_by(Assessment.submitted_at.desc().nullslast()).limit(500).all()
    return templates.TemplateResponse(
        request, "compliance_cases.html",
        {
            "user": user, "cases": rows, "org_names": _org_names(db),
            "filters": {"q": q, "level": level, "status": case_status,
                        "start": start, "end": end},
            "mask_name": mask_name, "mask_id_number": mask_id_number,
            "decrypt_pii": decrypt_pii,
        },
    )


@router.get("/cases/{case_no}", response_class=HTMLResponse)
def case_detail(request: Request, case_no: str, db: Session = Depends(get_db),
                user: User = Depends(require_oversight)):
    case = load_case(db, case_no, user)
    events = audit.trail(db, "assessment", case.case_no)
    q = case_context(db, case, user)["questionnaire"]
    context = case_context(db, case, user) | {
        "user": user,
        "trail": events,
        "parse_detail": audit.parse_detail,
        "approvals": case.approvals,
        "signals": anomalies.analyze_case(events, case, q.high_risk_threshold),
    }
    return templates.TemplateResponse(request, "compliance_case_detail.html", context)


@router.post("/cases/{case_no}/str")
def mark_str(
    request: Request,
    case_no: str,
    reference: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_compliance),
):
    """標記已向調查局申報疑似洗錢交易（範本第九點）。"""
    case = load_case(db, case_no, user)
    case.str_reported = True
    case.str_reported_at = utcnow()
    case.str_reference = reference.strip() or None
    audit.record(db, actor=user, action="assessment.str_reported", entity_type="assessment",
                 entity_id=case.case_no, detail={"reference": case.str_reference},
                 ip=client_ip(request))
    db.commit()
    return RedirectResponse(f"/compliance/cases/{case_no}", status_code=303)


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_oversight)):
    entries = (
        db.query(WatchListEntry)
        .filter(WatchListEntry.active.is_(True))
        .order_by(WatchListEntry.list_type, WatchListEntry.value)
        .all()
    )
    counts = dict(
        db.query(WatchListEntry.list_type, func.count(WatchListEntry.id))
        .filter(WatchListEntry.active.is_(True))
        .group_by(WatchListEntry.list_type)
        .all()
    )
    return templates.TemplateResponse(
        request, "watchlist.html",
        {"user": user, "entries": entries, "counts": counts,
         "list_types": screening.LIST_TYPES,
         "can_edit": user.role in (Role.COMPLIANCE, Role.ADMIN)},
    )


@router.post("/watchlist")
def add_watchlist(
    request: Request,
    list_type: str = Form(...),
    values: str = Form(...),
    source: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_compliance),
):
    if list_type not in screening.LIST_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "無效的名單類別")
    added = 0
    # 只以真正的換行切分。str.splitlines() 還會在 U+0085、U+2028 等字元切開，
    # 而洗防人員常直接從主管機關函令的 PDF 貼上名單，那些字元會夾帶進來。
    normalized = values.replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        value = line.strip()
        if value:
            screening.upsert(db, list_type, value, source.strip() or None)
            added += 1
    audit.record(db, actor=user, action="watchlist.add", entity_type="watchlist",
                 entity_id=list_type, detail={"count": added, "source": source.strip() or None},
                 ip=client_ip(request))
    db.commit()
    return RedirectResponse("/compliance/watchlist", status_code=303)


@router.post("/watchlist/{entry_id}/remove")
def remove_watchlist(request: Request, entry_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_compliance)):
    entry = db.get(WatchListEntry, entry_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "查無此名單項目")
    entry.active = False  # 停用而非刪除，保留曾經比對過的依據
    audit.record(db, actor=user, action="watchlist.remove", entity_type="watchlist",
                 entity_id=str(entry_id), detail={"value": entry.value}, ip=client_ip(request))
    db.commit()
    return RedirectResponse("/compliance/watchlist", status_code=303)


@router.get("/anomalies", response_class=HTMLResponse)
def anomaly_list(request: Request, days: int = Query(90, ge=1, le=730),
                 db: Session = Depends(get_db), user: User = Depends(require_oversight)):
    signals = anomalies.scan(db, since=utcnow() - timedelta(days=days), limit=500)
    return templates.TemplateResponse(
        request, "compliance_anomalies.html",
        {"user": user, "signals": signals, "days": days},
    )
