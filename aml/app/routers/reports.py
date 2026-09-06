"""報表匯出：董事會定期報告、年度主管機關報表、案件清冊。"""
from __future__ import annotations

import hashlib
import io
from datetime import UTC, date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import can_unmask_pii, require_oversight
from ..models import Assessment, OrgUnit, ReportExport, User
from ..services import aggregate, audit, exporters
from ..templating import templates
from .auth import client_ip

router = APIRouter(prefix="/reports")

COMPANY_NAME = get_settings().company_name
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _stream(wb: Workbook, filename: str, fallback: str) -> tuple[StreamingResponse, str]:
    buffer = io.BytesIO()
    wb.save(buffer)
    data = buffer.getvalue()
    checksum = hashlib.sha256(data).hexdigest()
    # HTTP header 只能放 latin-1，中文檔名須依 RFC 5987 以 filename* 傳遞；
    # 同時保留 ASCII 的 filename 供舊版瀏覽器回退。
    disposition = (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    response = StreamingResponse(
        io.BytesIO(data), media_type=XLSX_MEDIA,
        headers={"Content-Disposition": disposition},
    )
    return response, checksum


def _log_export(db: Session, request: Request, user: User, report_type: str, label: str,
                start: date, end: date, rows: int, checksum: str) -> None:
    db.add(
        ReportExport(
            report_type=report_type, period_label=label, period_start=start, period_end=end,
            generated_by=user.id, row_count=rows, checksum=checksum,
        )
    )
    audit.record(db, actor=user, action="report.export", entity_type="report",
                 entity_id=f"{report_type}:{label}",
                 detail={"rows": rows, "checksum": checksum}, ip=client_ip(request))
    db.commit()


def _cases_in_period(db: Session, start: date, end: date) -> list[Assessment]:
    from datetime import datetime, time

    return (
        db.query(Assessment)
        .filter(
            Assessment.status.in_(aggregate.COUNTED_STATUSES),
            Assessment.submitted_at >= datetime.combine(start, time.min, tzinfo=UTC),
            Assessment.submitted_at <= datetime.combine(end, time.max, tzinfo=UTC),
        )
        .order_by(Assessment.submitted_at.asc())
        .all()
    )


def _org_names(db: Session) -> dict[int, str]:
    return {o.id: o.name for o in db.query(OrgUnit).all()}


@router.get("", response_class=HTMLResponse)
def report_home(request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_oversight)):
    today = date.today()
    recent = (
        db.query(ReportExport).order_by(ReportExport.generated_at.desc()).limit(30).all()
    )
    return templates.TemplateResponse(
        request, "reports.html",
        {
            "user": user, "today": today, "recent": recent,
            "roc_year": today.year - 1911,
            "default_start": today.replace(month=1, day=1),
        },
    )


@router.get("/board.xlsx")
def board_report(
    request: Request,
    start: date = Query(...),
    end: date = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_oversight),
):
    """董事會定期報告。期間可自訂（月、季或半年）。"""
    summary = aggregate.summarize(db, start, end)
    cases = _cases_in_period(db, start, end)
    wb = exporters.build_board_workbook(summary, cases, COMPANY_NAME, _org_names(db))
    label = f"{start:%Y%m%d}-{end:%Y%m%d}"
    response, checksum = _stream(wb, f"董事會報告_客戶洗錢風險評估_{label}.xlsx",
                                 f"board-report-{label}.xlsx")
    _log_export(db, request, user, "board", label, start, end, summary.total_cases, checksum)
    return response


@router.get("/annual.xlsx")
def annual_report(
    request: Request,
    roc_year: int = Query(..., ge=100, le=200, description="民國年，例如 115"),
    db: Session = Depends(get_db),
    user: User = Depends(require_oversight),
):
    """年度風險評估彙總表，版面比照主管機關 115 年格式。"""
    year = roc_year + 1911
    start, end = date(year, 1, 1), date(year, 12, 31)
    summary = aggregate.summarize(db, start, end)
    wb = exporters.build_annual_workbook(summary, COMPANY_NAME)
    label = f"{roc_year}"
    response, checksum = _stream(wb, f"{roc_year}年度洗錢及資恐風險評估彙總表.xlsx",
                                 f"annual-report-roc{roc_year}.xlsx")
    _log_export(db, request, user, "annual", label, start, end, summary.total_cases, checksum)
    return response


@router.get("/register.xlsx")
def case_register(
    request: Request,
    start: date = Query(...),
    end: date = Query(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_oversight),
):
    """案件清冊。個資是否揭露明文依角色決定。"""
    cases = _cases_in_period(db, start, end)
    wb = exporters.build_case_register(cases, _org_names(db), unmask=can_unmask_pii(user))
    label = f"{start:%Y%m%d}-{end:%Y%m%d}"
    response, checksum = _stream(wb, f"客戶風險評估案件清冊_{label}.xlsx",
                                 f"case-register-{label}.xlsx")
    _log_export(db, request, user, "register", label, start, end, len(cases), checksum)
    return response
