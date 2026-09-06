"""單位主管簽核。

範本第五點第一款第一目：高風險客戶於建立或新增業務往來關係前，
應取得高階管理人員同意。內控手冊 BIC06-03 八(二)1 進一步指定為營運中心主管。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_roles
from ..models import Approval, Assessment, AssessmentStatus, Role, User
from ..services import audit
from ..templating import templates
from .assessments import case_context, load_case
from .auth import client_ip

router = APIRouter()
_reviewer = require_roles(Role.SUPERVISOR, Role.COMPLIANCE, Role.ADMIN)


@router.get("/review", response_class=HTMLResponse)
def pending(request: Request, db: Session = Depends(get_db), user: User = Depends(_reviewer)):
    query = db.query(Assessment).filter(
        Assessment.status.in_((AssessmentStatus.PENDING_APPROVAL, AssessmentStatus.BLOCKED))
    )
    if user.role == Role.SUPERVISOR:
        query = query.filter(Assessment.org_unit_id == user.org_unit_id)
    cases = query.order_by(Assessment.submitted_at.asc()).all()
    return templates.TemplateResponse(
        request, "review_list.html",
        {
            "user": user,
            "pending": [c for c in cases if c.status == AssessmentStatus.PENDING_APPROVAL],
            "blocked": [c for c in cases if c.status == AssessmentStatus.BLOCKED],
        },
    )


@router.get("/review/{case_no}", response_class=HTMLResponse)
def review_detail(request: Request, case_no: str, db: Session = Depends(get_db),
                  user: User = Depends(_reviewer)):
    case = load_case(db, case_no, user)
    context = case_context(db, case, user) | {
        "user": user,
        "trail": audit.trail(db, "assessment", case.case_no),
        "approvals": case.approvals,
    }
    return templates.TemplateResponse(request, "review_detail.html", context)


@router.post("/review/{case_no}/decision")
def decide(
    request: Request,
    case_no: str,
    decision: str = Form(...),
    comment: str = Form(""),
    wealth_source: str = Form(""),
    fund_source_detail: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(_reviewer),
):
    case = load_case(db, case_no, user)
    if case.status != AssessmentStatus.PENDING_APPROVAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "此案件目前狀態不需簽核")
    if decision not in ("approved", "rejected"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "無效的簽核決定")

    # 同意建立業務關係前，強化措施的兩項紀錄不得留白（範本第五點第一款第二目）。
    if decision == "approved":
        case.wealth_source = (wealth_source or case.wealth_source or "").strip() or None
        case.fund_source_detail = (
            fund_source_detail or case.fund_source_detail or ""
        ).strip() or None
        if not case.wealth_source or not case.fund_source_detail:
            context = case_context(db, case, user) | {
                "user": user,
                "trail": audit.trail(db, "assessment", case.case_no),
                "approvals": case.approvals,
                "error": "同意前應填列客戶財富來源與資金之實質來源（範本第五點第一款第二目）",
            }
            return templates.TemplateResponse(request, "review_detail.html", context,
                                              status_code=400)

    db.add(
        Approval(
            assessment_id=case.id,
            approver_id=user.id,
            approver_role=user.role,
            decision=decision,
            comment=comment.strip() or None,
        )
    )
    case.status = (
        AssessmentStatus.APPROVED if decision == "approved" else AssessmentStatus.REJECTED
    )
    audit.record(
        db, actor=user, action=f"assessment.{decision}", entity_type="assessment",
        entity_id=case.case_no,
        detail={"comment": comment.strip() or None, "role": user.role.value},
        ip=client_ip(request),
    )
    db.commit()
    return RedirectResponse("/review", status_code=303)
