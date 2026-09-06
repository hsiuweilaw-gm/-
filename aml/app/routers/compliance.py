"""洗防專責後台：即時監控、案件查詢、STR 標記、名單維護、行為異常檢視。"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_compliance, require_oversight
from ..models import (
    Assessment,
    AssessmentStatus,
    OrgUnit,
    PeriodicReview,
    ReviewOutcome,
    RiskLevel,
    Role,
    User,
    WatchListEntry,
    WatchListName,
    as_aware,
    utcnow,
)
from ..security import decrypt_pii, mask_id_number, mask_name
from ..services import (
    aggregate,
    anomalies,
    assessments,
    audit,
    login_anomalies,
    reviews,
    sanctions_import,
    screening,
)
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
    # 曾命中名單的案件。包含草稿——業務員看到「應婉拒」後放棄草稿另建新案，
    # 是最需要被看見的規避手法，而放棄的草稿不會出現在任何其他清單裡。
    watchlist_hits = (
        db.query(Assessment)
        .filter(Assessment.watchlist_hit_at.isnot(None))
        .order_by(Assessment.watchlist_hit_at.desc())
        .limit(50)
        .all()
    )
    abandoned = [
        c for c in watchlist_hits
        if c.status == AssessmentStatus.DRAFT
        and as_aware(c.updated_at) < utcnow() - timedelta(hours=24)
    ]

    overdue_reviews = reviews.overdue_count(db)

    # 只掃描近 90 天，避免每次開啟儀表板都全表掃描。
    signals = anomalies.scan(db, since=utcnow() - timedelta(days=90), limit=200)
    login_signals = login_anomalies.scan(db, since=utcnow() - timedelta(days=30), limit=200)

    return templates.TemplateResponse(
        request, "compliance_dashboard.html",
        {
            "user": user,
            "summary": summary,
            "queue": queue,
            "str_pending": str_pending,
            "watchlist_hits": watchlist_hits,
            "overdue_reviews": overdue_reviews,
            "abandoned": abandoned,
            "signals": signals[:20],
            "signal_total": len(signals),
            "login_signal_total": len(login_signals),
            "login_signal_serious": sum(1 for s in login_signals if s.severity >= 3),
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


@router.post("/cases/{case_no}/hit-review")
def review_watchlist_hit(
    request: Request,
    case_no: str,
    decision: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_compliance),
):
    """對曾命中制裁／資恐名單之案件作成覆核結論。

    決定為 confirmed 者依範本第四點婉拒建立業務關係；為 cleared 者
    案件回到原本應有的流程（高風險仍須主管同意）。
    """
    case = load_case(db, case_no, user)
    try:
        assessments.clear_hit_review(
            db, case, user,
            confirmed_match=(decision == "confirmed"),
            note=note,
            ip=client_ip(request),
        )
    except ValueError as exc:
        context = case_context(db, case, user) | {
            "user": user,
            "trail": audit.trail(db, "assessment", case.case_no),
            "parse_detail": audit.parse_detail,
            "approvals": case.approvals,
            "signals": [],
            "hit_review_error": str(exc),
        }
        return templates.TemplateResponse(
            request, "compliance_case_detail.html", context, status_code=400
        )
    return RedirectResponse(f"/compliance/cases/{case_no}", status_code=303)


def _watchlist_context(db: Session, user: User, **extra) -> dict:
    """完整制裁名單可達數萬筆，清單只顯示最近 200 筆；查特定對象請用搜尋。"""
    keyword = (extra.get("keyword") or "").strip()
    query = db.query(WatchListEntry).filter(WatchListEntry.active.is_(True))
    if keyword:
        normalized = screening.normalize(keyword)
        query = query.join(WatchListName).filter(
            WatchListName.normalized.like(f"%{normalized}%")
        )
    entries = query.order_by(WatchListEntry.id.desc()).limit(200).all()
    total = (
        db.query(func.count(WatchListEntry.id))
        .filter(WatchListEntry.active.is_(True)).scalar()
    )
    return {
        "user": user,
        "entries": entries,
        "total": total,
        "summary": sanctions_import.summary(db),
        "list_types": screening.LIST_TYPES,
        "screened_types": screening.SCREENED_LIST_TYPES,
        "can_edit": user.role in (Role.COMPLIANCE, Role.ADMIN),
    } | extra


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, q: str = Query("", max_length=64),
              db: Session = Depends(get_db), user: User = Depends(require_oversight)):
    return templates.TemplateResponse(
        request, "watchlist.html", _watchlist_context(db, user, keyword=q)
    )


@router.post("/watchlist/import", response_class=HTMLResponse)
async def import_watchlist(
    request: Request,
    file: UploadFile = File(...),
    keep_old: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_compliance),
):
    """上傳制裁名單檔（xlsx 或 csv）。"""
    raw = await file.read()
    filename = (file.filename or "").lower()
    try:
        if filename.endswith((".xlsx", ".xlsm")):
            rows = sanctions_import.rows_from_xlsx(raw)
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("cp950")
            rows = sanctions_import.rows_from_csv(text)
        result = sanctions_import.import_rows(db, rows, replace_sources=not keep_old)
    except Exception as exc:  # noqa: BLE001 檔案由人上傳，解析失敗要回報而非 500
        db.rollback()
        return templates.TemplateResponse(
            request, "watchlist.html",
            _watchlist_context(db, user, import_error=f"檔案無法解析：{exc}"),
            status_code=400,
        )

    if not result.ok:
        db.rollback()
        return templates.TemplateResponse(
            request, "watchlist.html",
            _watchlist_context(db, user, import_error="；".join(result.errors)),
            status_code=400,
        )

    audit.record(
        db, actor=user, action="watchlist.import", entity_type="watchlist",
        entity_id=result.batch,
        detail={"file": file.filename, "entries": result.entries, "names": result.names,
                "by_source": result.by_source, "deactivated": result.deactivated},
        ip=client_ip(request),
    )
    db.commit()
    return templates.TemplateResponse(
        request, "watchlist.html", _watchlist_context(db, user, import_result=result)
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
    since = utcnow() - timedelta(days=days)
    return templates.TemplateResponse(
        request, "compliance_anomalies.html",
        {
            "user": user,
            "days": days,
            "signals": anomalies.scan(db, since=since, limit=500),
            "login_signals": login_anomalies.scan(db, since=since, limit=200),
        },
    )


@router.get("/reviews", response_class=HTMLResponse)
def review_queue(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_oversight)):
    """定期審查待辦。"""
    due = reviews.due_cases(db)
    today = date.today()
    return templates.TemplateResponse(
        request, "compliance_reviews.html",
        {
            "user": user,
            "due": due,
            "today": today,
            "overdue": [c for c in due if c.review_due_on and c.review_due_on < today],
            "org_names": _org_names(db),
            "outcomes": list(ReviewOutcome),
            "can_review": user.role in (Role.COMPLIANCE, Role.ADMIN),
            "recent": (
                db.query(PeriodicReview)
                .order_by(PeriodicReview.performed_at.desc())
                .limit(30)
                .all()
            ),
        },
    )


@router.post("/reviews/rescreen")
def run_rescreen(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_compliance)):
    """以當下名單重新篩檢所有仍在監督中的案件。"""
    result = reviews.rescreen(db, actor=user, ip=client_ip(request))
    return RedirectResponse(
        f"/compliance/reviews?checked={result.checked}&hits={result.hit_count}",
        status_code=303,
    )


@router.post("/reviews/{case_no}")
def record_review(
    request: Request,
    case_no: str,
    outcome: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_compliance),
):
    case = load_case(db, case_no, user)
    try:
        decision = ReviewOutcome(outcome)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "無效的審查結論") from exc
    reviews.record(db, case, user, decision, note, ip=client_ip(request))
    return RedirectResponse("/compliance/reviews", status_code=303)
