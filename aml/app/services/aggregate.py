"""風險彙總。

支援兩種報表：
  1. 董事會定期報告 — 期間內的風險分布、高風險與擋件案件、待辦事項。
  2. 年度主管機關報表 — 完全比照主管機關 115 年格式（件數／占比／
     平均風險分數／風險等級；產品風險另需以保費金額加權）。

彙總維度與權重全部由問卷定義檔的 annual_key / annual_bucket 推導，
問卷改版時報表自動跟著改，不需要改這支程式。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Answer, Assessment, AssessmentStatus, RiskLevel
from ..scoring.engine import Questionnaire, load_questionnaire

# 已送出的案件才計入報表；草稿不算招攬新契約。
COUNTED_STATUSES = (
    AssessmentStatus.SUBMITTED,
    AssessmentStatus.PENDING_APPROVAL,
    AssessmentStatus.APPROVED,
    AssessmentStatus.REJECTED,
    AssessmentStatus.BLOCKED,
)


@dataclass
class Bucket:
    label: str
    weight: int
    count: int = 0
    premium: float = 0.0


@dataclass
class Dimension:
    key: str
    label: str
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return sum(b.count for b in self.buckets)

    @property
    def total_premium(self) -> float:
        return sum(b.premium for b in self.buckets)

    def share(self, bucket: Bucket) -> float:
        return bucket.count / self.total_count if self.total_count else 0.0

    def premium_share(self, bucket: Bucket) -> float:
        return bucket.premium / self.total_premium if self.total_premium else 0.0

    @property
    def average_score(self) -> float:
        """件數加權平均風險分數。"""
        if not self.total_count:
            return 0.0
        return sum(b.weight * b.count for b in self.buckets) / self.total_count

    @property
    def premium_weighted_score(self) -> float:
        """保費金額加權平均風險分數（年度報表產品風險專用）。"""
        if not self.total_premium:
            return 0.0
        return sum(b.weight * b.premium for b in self.buckets) / self.total_premium


def risk_band(score: float) -> str:
    s = get_settings()
    if score >= s.aggregate_high_min:
        return "高"
    if score >= s.aggregate_medium_min:
        return "中"
    return "低" if score > 0 else "－"


@dataclass
class PeriodSummary:
    period_start: date
    period_end: date
    questionnaire: Questionnaire
    total_cases: int = 0
    total_premium: float = 0.0
    high_risk_cases: int = 0
    general_risk_cases: int = 0
    blocked_cases: int = 0
    pending_approval_cases: int = 0
    hit_review_cases: int = 0
    approved_high_risk: int = 0
    rejected_cases: int = 0
    override_cases: int = 0
    str_reported_cases: int = 0
    sanction_hits: int = 0
    offshore_remittance_cases: int = 0
    draft_cases: int = 0
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    by_org: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def high_risk_ratio(self) -> float:
        return self.high_risk_cases / self.total_cases if self.total_cases else 0.0


def _dimension_skeleton(q: Questionnaire) -> dict[str, Dimension]:
    dims: dict[str, Dimension] = {}
    for category in q.categories:
        for factor in category.factors:
            if not factor.annual_key:
                continue
            buckets = [
                Bucket(label=o.annual_bucket, weight=o.score)
                for o in factor.options
                if o.annual_bucket
            ]
            if factor.annual_order:
                order = {label: i for i, label in enumerate(factor.annual_order)}
                buckets.sort(key=lambda b: order.get(b.label, len(order)))
            dims[factor.annual_key] = Dimension(
                key=factor.annual_key, label=factor.label, buckets=buckets,
            )
    return dims


def _factor_by_annual_key(q: Questionnaire) -> dict[str, str]:
    return {f.annual_key: f.code for f in q.factors if f.annual_key}


def _as_utc(d: date, end: bool = False) -> datetime:
    return datetime.combine(d, time.max if end else time.min, tzinfo=UTC)


def summarize(
    db: Session, period_start: date, period_end: date, questionnaire_id: str = "life"
) -> PeriodSummary:
    q = load_questionnaire(questionnaire_id)
    summary = PeriodSummary(
        period_start=period_start, period_end=period_end, questionnaire=q,
        dimensions=_dimension_skeleton(q),
    )
    key_to_factor = _factor_by_annual_key(q)
    factor_to_key = {v: k for k, v in key_to_factor.items()}

    cases = (
        db.query(Assessment)
        .filter(
            Assessment.status.in_(COUNTED_STATUSES),
            Assessment.submitted_at >= _as_utc(period_start),
            Assessment.submitted_at <= _as_utc(period_end, end=True),
        )
        .all()
    )
    summary.draft_cases = (
        db.query(Assessment).filter(Assessment.status == AssessmentStatus.DRAFT).count()
    )

    case_ids = [c.id for c in cases]
    answers_by_case: dict[int, dict[str, str]] = defaultdict(dict)
    if case_ids:
        # 分批查詢，避免 300 人以上規模下 IN 子句過長。
        for start in range(0, len(case_ids), 500):
            chunk = case_ids[start : start + 500]
            for a in db.query(Answer).filter(Answer.assessment_id.in_(chunk)).all():
                answers_by_case[a.assessment_id][a.factor_code] = a.option_code

    bucket_lookup: dict[str, dict[str, str]] = {}
    for factor in q.factors:
        if factor.annual_key:
            bucket_lookup[factor.code] = {
                o.code: o.annual_bucket for o in factor.options if o.annual_bucket
            }

    org_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "high": 0, "blocked": 0}
    )

    for case in cases:
        summary.total_cases += 1
        premium = case.annual_premium or 0.0
        summary.total_premium += premium

        if case.risk_level == RiskLevel.HIGH:
            summary.high_risk_cases += 1
        else:
            summary.general_risk_cases += 1
        if case.status == AssessmentStatus.BLOCKED:
            summary.blocked_cases += 1
        elif case.status == AssessmentStatus.PENDING_APPROVAL:
            summary.pending_approval_cases += 1
        elif case.status == AssessmentStatus.HIT_REVIEW:
            summary.hit_review_cases += 1
        elif case.status == AssessmentStatus.APPROVED:
            summary.approved_high_risk += 1
        elif case.status == AssessmentStatus.REJECTED:
            summary.rejected_cases += 1
        if case.override_applied:
            summary.override_cases += 1
        if case.str_reported:
            summary.str_reported_cases += 1
        if case.offshore_remittance:
            summary.offshore_remittance_cases += 1

        org_key = str(case.org_unit_id or "未指定")
        org_stats[org_key]["total"] += 1
        if case.risk_level == RiskLevel.HIGH:
            org_stats[org_key]["high"] += 1
        if case.status == AssessmentStatus.BLOCKED:
            org_stats[org_key]["blocked"] += 1

        case_answers = answers_by_case.get(case.id, {})
        sanction_hit = False
        for factor_code, option_code in case_answers.items():
            annual_key = factor_to_key.get(factor_code)
            if not annual_key:
                continue
            bucket_label = bucket_lookup.get(factor_code, {}).get(option_code)
            if not bucket_label:
                continue
            if bucket_label == "制裁名單":
                sanction_hit = True
            dim = summary.dimensions[annual_key]
            for bucket in dim.buckets:
                if bucket.label == bucket_label:
                    bucket.count += 1
                    bucket.premium += premium
                    break
        if sanction_hit:
            summary.sanction_hits += 1

    summary.by_org = dict(org_stats)
    return summary
