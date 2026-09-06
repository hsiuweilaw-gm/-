"""自動儲存 API。

業務員在客戶面前填寫，每一次點選都立即寫入資料庫並回傳最新分數，
避免關閉頁面、網路中斷或換裝置造成資料遺失。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import can_edit_assessment, can_see_score, current_user
from ..models import Assessment, User
from ..scoring.engine import ScoreResult
from ..services import assessments as svc
from .assessments import load_case
from .auth import client_ip

router = APIRouter(prefix="/api")


class AnswerIn(BaseModel):
    factor: str = Field(min_length=1, max_length=64)
    option: str = Field(min_length=1, max_length=64)


class ChecksIn(BaseModel):
    group: str = Field(pattern="^(refusal|mandatory|suspicious)$")
    codes: list[str] = Field(default_factory=list, max_length=64)


class ConsultIn(BaseModel):
    supervisor_name: str = Field(min_length=1, max_length=64)


class ProfileIn(BaseModel):
    holder_name: str | None = Field(default=None, max_length=128)
    holder_id: str | None = Field(default=None, max_length=32)
    insured_name: str | None = Field(default=None, max_length=128)
    beneficiary_name: str | None = Field(default=None, max_length=128)
    insurer_name: str | None = Field(default=None, max_length=128)
    policy_no: str | None = Field(default=None, max_length=64)
    annual_premium: str | None = Field(default=None, max_length=24)
    wealth_source: str | None = Field(default=None, max_length=2000)
    fund_source_detail: str | None = Field(default=None, max_length=2000)


def _editable(user: User, case: Assessment) -> None:
    if not can_edit_assessment(user, case):
        raise HTTPException(status.HTTP_409_CONFLICT, "案件已送出或非本人承辦，無法修改")


def _payload(result: ScoreResult, assessment: Assessment, *, reveal_score: bool) -> dict:
    """組出回傳給前端的評分結果。

    業務員不得知悉分數與風險等級（公司政策：避免為規避強化盡職調查而調整作答），
    因此僅回傳填答進度、是否應婉拒、以及是否須照會主管。
    分數僅提供給第二、三道防線與主管。
    """
    payload = {
        "answered": result.answered,
        "total_factors": result.total_factors,
        "complete": result.complete,
        "missing_factors": result.missing_factors,
        "blocked": result.blocked,
        "blocked_reasons": result.blocked_reasons,
        # 須照會主管：等級為高風險且非應婉拒之情形
        "needs_consultation": result.level == "high" and not result.blocked,
        "consulted": bool(assessment.consulted_supervisor),
        "consulted_name": assessment.consulted_name,
    }
    if not reveal_score:
        return payload
    return payload | {
        "total_score": result.total_score,
        "min_score": result.min_score,
        "max_score": result.max_score,
        "threshold": result.threshold,
        "level": result.level,
        "level_label": result.level_label,
        "override_applied": result.override_applied,
        "override_reasons": result.override_reasons,
        "category_scores": [
            {"code": c.code, "label": c.label, "score": c.score, "max_score": c.max_score}
            for c in result.category_scores
        ],
    }


@router.get("/assessments/{case_no}/status")
def get_status(case_no: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    result, _ = svc.evaluate_with_screening(db, case)
    return _payload(result, case, reveal_score=can_see_score(user))


@router.post("/assessments/{case_no}/answer")
def save_answer(case_no: str, body: AnswerIn, request: Request,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    _editable(user, case)
    try:
        result = svc.save_answer(db, case, user, body.factor, body.option, ip=client_ip(request))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _payload(result, case, reveal_score=can_see_score(user))


@router.post("/assessments/{case_no}/checks")
def save_checks(case_no: str, body: ChecksIn, request: Request,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    _editable(user, case)
    result = svc.save_checks(db, case, user, body.group, body.codes, ip=client_ip(request))
    return _payload(result, case, reveal_score=can_see_score(user))


@router.post("/assessments/{case_no}/profile")
def save_profile(case_no: str, body: ProfileIn, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    case = load_case(db, case_no, user)
    _editable(user, case)
    try:
        result = svc.save_profile(
            db, case, user, body.model_dump(exclude_none=True), ip=client_ip(request)
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # 姓名可能命中名單，故一併回傳最新狀態讓畫面即時更新。
    return _payload(result, case, reveal_score=can_see_score(user))


@router.post("/assessments/{case_no}/consult")
def record_consultation(case_no: str, body: ConsultIn, request: Request,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    """業務員確認已照會單位主管。高風險案件須完成此步驟才能送出。"""
    case = load_case(db, case_no, user)
    _editable(user, case)
    try:
        svc.record_consultation(db, case, user, body.supervisor_name, ip=client_ip(request))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    result, _ = svc.evaluate_with_screening(db, case)
    return _payload(result, case, reveal_score=can_see_score(user))
