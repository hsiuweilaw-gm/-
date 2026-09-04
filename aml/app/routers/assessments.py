"""業務員前台：填寫評估、送出、檢視自己的案件。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import (
    can_edit_assessment,
    can_see_score,
    can_unmask_pii,
    can_view_assessment,
    current_user,
)
from ..models import Assessment, AssessmentStatus, User
from ..security import decrypt_pii, mask_id_number, mask_name
from ..services import assessments as svc
from ..templating import templates
from .auth import client_ip

router = APIRouter()


def load_case(db: Session, case_no: str, user: User) -> Assessment:
    case = db.query(Assessment).filter(Assessment.case_no == case_no).one_or_none()
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "查無此案件")
    if not can_view_assessment(user, case):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "無權檢視此案件")
    return case


def case_context(db: Session, case: Assessment, user: User) -> dict:
    """組出樣板所需的案件資料。個資依角色決定是否遮罩。

    評分一律採含名單比對的版本：名單更新後重新開啟舊案件時，
    畫面必須反映新名單的比對結果。
    """
    unmask = can_unmask_pii(user) or case.agent_id == user.id
    holder_name = decrypt_pii(case.holder_name_enc)
    holder_id = decrypt_pii(case.holder_id_enc)
    result, watchlist_hits = svc.evaluate_with_screening(db, case)
    return {
        "case": case,
        "questionnaire": svc.questionnaire_for(case),
        "answers": svc.answers_map(case),
        "checks": svc.stored_checks(case),
        "result": result,
        "watchlist_hits": watchlist_hits,
        # 業務員不得知悉分數與等級；樣板一律以此旗標決定是否呈現。
        "show_score": can_see_score(user),
        "holder_name": holder_name if unmask else mask_name(holder_name),
        "holder_id": holder_id if unmask else mask_id_number(holder_id),
        "insured_name": decrypt_pii(case.insured_name_enc) if unmask else
                        mask_name(decrypt_pii(case.insured_name_enc)),
        "beneficiary_name": decrypt_pii(case.beneficiary_name_enc) if unmask else
                            mask_name(decrypt_pii(case.beneficiary_name_enc)),
        "editable": can_edit_assessment(user, case),
    }


@router.get("/assessments", response_class=HTMLResponse)
def my_cases(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cases = (
        db.query(Assessment)
        .filter(Assessment.agent_id == user.id)
        .order_by(Assessment.updated_at.desc())
        .limit(200)
        .all()
    )
    drafts = [c for c in cases if c.status == AssessmentStatus.DRAFT]
    # 業務員檢視自己承辦的案件，姓名不遮罩；解密在此一次完成，樣板不碰密文。
    holder_names = {c.id: decrypt_pii(c.holder_name_enc) for c in cases}
    return templates.TemplateResponse(
        request, "agent_list.html",
        {"user": user, "cases": cases, "drafts": drafts, "holder_names": holder_names},
    )


@router.post("/assessments/new")
def new_case(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = svc.create_draft(db, user, ip=client_ip(request))
    return RedirectResponse(f"/assessments/{case.case_no}", status_code=303)


@router.get("/assessments/{case_no}", response_class=HTMLResponse)
def case_form(request: Request, case_no: str, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    context = case_context(db, case, user) | {"user": user}
    return templates.TemplateResponse(request, "assessment_form.html", context)


@router.post("/assessments/{case_no}/submit")
def submit_case(request: Request, case_no: str, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    if not can_edit_assessment(user, case):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "此案件已送出或非本人承辦，無法再修改")
    try:
        svc.submit(db, case, user, ip=client_ip(request))
    except ValueError as exc:
        context = case_context(db, case, user) | {"user": user, "error": str(exc)}
        return templates.TemplateResponse(request, "assessment_form.html", context, status_code=400)
    return RedirectResponse(f"/assessments/{case_no}/result", status_code=303)


@router.get("/assessments/{case_no}/result", response_class=HTMLResponse)
def case_result(request: Request, case_no: str, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    context = case_context(db, case, user) | {"user": user}
    return templates.TemplateResponse(request, "assessment_result.html", context)
