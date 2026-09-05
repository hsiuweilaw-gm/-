"""既有客戶的持續監督。

招攬當下評估一次不足以滿足法規要求：

  * 範本第五點第一款第三目：對於業務往來關係應採取強化之持續監督。
  * 範本第二點第二款第四目：對過去所取得客戶身分資料之真實性或妥適性有所懷疑時，
    應確認客戶身分。
  * 問答集 Q8：應定期檢視所辨識之客戶及實質受益人身分資料是否足夠並確保更新，
    特別是高風險客戶。

本模組提供兩件事：

  1. **批次重新篩檢**——名單每月更新，既有客戶可能在事後才被列入。
     這是自動的，也是兩者中防護價值較高的：沒有它，只有在有人剛好重新開啟舊案件時
     才會發現，而沒有人會主動去開。
  2. **週期性審查**——依風險等級排定應審查日，到期由洗防專責人員複核並記錄結論。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Assessment,
    AssessmentStatus,
    PeriodicReview,
    ReviewOutcome,
    RiskLevel,
    User,
    utcnow,
)
from . import audit
from .assessments import mark_watchlist_hit, screen_case

# 已結案與擋件案件不需持續監督：前者業務關係已結束，後者本就不得建立業務關係。
MONITORED_STATUSES = (
    AssessmentStatus.SUBMITTED,
    AssessmentStatus.HIT_REVIEW,
    AssessmentStatus.PENDING_APPROVAL,
    AssessmentStatus.APPROVED,
)


def _add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year = start.year + month // 12
    month = month % 12 + 1
    day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def review_interval_months(level: RiskLevel | None) -> int:
    settings = get_settings()
    if level == RiskLevel.HIGH:
        return settings.review_months_high
    return settings.review_months_general


def schedule_next(assessment: Assessment, *, from_date: date | None = None) -> date:
    """依風險等級排定下次應審查日。"""
    base = from_date or date.today()
    due = _add_months(base, review_interval_months(assessment.risk_level))
    assessment.review_due_on = due
    return due


def due_cases(db: Session, as_of: date | None = None, limit: int = 200) -> list[Assessment]:
    """已到期或逾期未審查的案件，最舊的排前面。"""
    as_of = as_of or date.today()
    return (
        db.query(Assessment)
        .filter(Assessment.status.in_(MONITORED_STATUSES),
                Assessment.review_due_on.isnot(None),
                Assessment.review_due_on <= as_of)
        .order_by(Assessment.review_due_on.asc())
        .limit(limit)
        .all()
    )


def overdue_count(db: Session, as_of: date | None = None) -> int:
    as_of = as_of or date.today()
    return (
        db.query(Assessment)
        .filter(Assessment.status.in_(MONITORED_STATUSES),
                Assessment.review_due_on.isnot(None),
                Assessment.review_due_on < as_of)
        .count()
    )


@dataclass
class RescreenResult:
    checked: int = 0
    new_hits: list[Assessment] = field(default_factory=list)

    @property
    def hit_count(self) -> int:
        return len(self.new_hits)


def rescreen(db: Session, actor: User | None = None, limit: int | None = None,
             ip: str | None = None) -> RescreenResult:
    """以當下的名單重新篩檢所有仍在監督中的案件。

    只標記「先前未命中、現在命中」者——先前已命中的案件早已在洗防的待辦清單上。
    命中留痕沿用 mark_watchlist_hit，因此結果會直接出現在儀表板的命中清單。
    """
    query = (
        db.query(Assessment)
        .filter(Assessment.status.in_(MONITORED_STATUSES),
                Assessment.watchlist_hit_at.is_(None))
        .order_by(Assessment.id.asc())
    )
    if limit:
        query = query.limit(limit)

    result = RescreenResult()
    now = utcnow()
    for case in query.all():
        result.checked += 1
        case.rescreened_at = now
        hits = screen_case(db, case)
        if hits:
            mark_watchlist_hit(case, hits)
            result.new_hits.append(case)
            audit.record(
                db, actor=actor, action="assessment.rescreen_hit", entity_type="assessment",
                entity_id=case.case_no,
                detail={"hits": [h.describe for h in hits[:5]]}, ip=ip,
            )

    audit.record(
        db, actor=actor, action="watchlist.rescreen", entity_type="watchlist",
        entity_id="rescreen",
        detail={"checked": result.checked, "new_hits": result.hit_count}, ip=ip,
    )
    db.commit()
    return result


def record(db: Session, assessment: Assessment, actor: User, outcome: ReviewOutcome,
           note: str | None = None, ip: str | None = None) -> PeriodicReview:
    """記錄一次定期審查，並排定下次應審查日。"""
    before = assessment.risk_level
    after = before
    if outcome == ReviewOutcome.ESCALATED:
        after = RiskLevel.HIGH
    elif outcome == ReviewOutcome.DEESCALATED:
        after = RiskLevel.GENERAL

    today = date.today()
    # 先取下本次審查的到期日，再更動案件——否則終止時會先被清成 None。
    due_on = assessment.review_due_on or today

    assessment.risk_level = after
    assessment.last_reviewed_on = today
    if outcome == ReviewOutcome.TERMINATED:
        assessment.status = AssessmentStatus.CLOSED
        assessment.review_due_on = None
        next_due = None
    else:
        next_due = schedule_next(assessment, from_date=today)

    review = PeriodicReview(
        assessment_id=assessment.id,
        due_on=due_on,
        performed_by=actor.id,
        outcome=outcome,
        risk_level_before=before,
        risk_level_after=after,
        watchlist_hit=assessment.watchlist_hit_at is not None,
        note=(note or "").strip() or None,
        next_due_on=next_due,
    )
    db.add(review)
    audit.record(
        db, actor=actor, action="assessment.periodic_review", entity_type="assessment",
        entity_id=assessment.case_no,
        detail={"outcome": outcome.value, "before": before.value if before else None,
                "after": after.value if after else None,
                "next_due_on": next_due.isoformat() if next_due else None,
                "note": review.note},
        ip=ip,
    )
    db.commit()
    return review
