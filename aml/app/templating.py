"""Jinja2 環境與樣板輔助函式。"""
from __future__ import annotations

import json
from typing import Any

from fastapi.templating import Jinja2Templates

from .models import AssessmentStatus, ReviewOutcome, RiskLevel, Role
from .services.audit import parse_detail

templates = Jinja2Templates(directory="app/templates")

STATUS_LABELS = {
    AssessmentStatus.DRAFT: "填寫中",
    AssessmentStatus.SUBMITTED: "已完成",
    AssessmentStatus.PENDING_APPROVAL: "待主管同意",
    AssessmentStatus.APPROVED: "主管已同意",
    AssessmentStatus.REJECTED: "主管不同意",
    AssessmentStatus.BLOCKED: "已擋件",
    AssessmentStatus.CLOSED: "結案",
}
STATUS_TONE = {
    AssessmentStatus.DRAFT: "muted",
    AssessmentStatus.SUBMITTED: "ok",
    AssessmentStatus.PENDING_APPROVAL: "warn",
    AssessmentStatus.APPROVED: "ok",
    AssessmentStatus.REJECTED: "danger",
    AssessmentStatus.BLOCKED: "danger",
    AssessmentStatus.CLOSED: "muted",
}
ROLE_LABELS = {
    Role.AGENT: "業務人員",
    Role.SUPERVISOR: "單位主管",
    Role.COMPLIANCE: "洗防專責",
    Role.AUDITOR: "內部稽核",
    Role.ADMIN: "系統管理者",
}
LEVEL_LABELS = {RiskLevel.HIGH: "高風險", RiskLevel.GENERAL: "一般風險"}
REVIEW_OUTCOMES = {
    ReviewOutcome.UNCHANGED: "維持原風險等級",
    ReviewOutcome.ESCALATED: "調升為高風險",
    ReviewOutcome.DEESCALATED: "調降為一般風險",
    ReviewOutcome.REASSESS: "應重新辦理客戶審查",
    ReviewOutcome.TERMINATED: "終止業務關係",
}


def json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    return value if isinstance(value, list) else [value]


def money(value: float | int | None) -> str:
    return f"{value:,.0f}" if value else "－"


templates.env.filters["json_list"] = json_list
templates.env.filters["money"] = money
templates.env.globals.update(
    parse_detail=parse_detail,
    STATUS_LABELS=STATUS_LABELS,
    STATUS_TONE=STATUS_TONE,
    ROLE_LABELS=ROLE_LABELS,
    LEVEL_LABELS=LEVEL_LABELS,
    REVIEW_OUTCOMES=REVIEW_OUTCOMES,
    ReviewOutcome=ReviewOutcome,
    Role=Role,
    AssessmentStatus=AssessmentStatus,
    RiskLevel=RiskLevel,
)
