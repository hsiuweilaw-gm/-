"""業務端作答行為異常偵測。

系統依需求向業務員即時揭露分數與風險等級。揭露有其必要（業務員須當場
知悉是否應通知主管列管），但也讓「湊分數」變得可能：把總分壓在門檻
之下即可規避高風險的強化盡職調查。

因此第二道防線需要能看見作答過程，而不只是最終結果。以下指標全部
取自唯增的稽核軌跡，業務員無從修改。
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Assessment, AssessmentStatus, AuditEvent, RiskLevel

# 送出前於門檻上方停留過、最終卻落在門檻下方，視為需要複核的訊號。
NEAR_THRESHOLD_MARGIN = 3
# 同一題反覆改寫達此次數即列為訊號。
REVISION_ALERT = 3
# 全表 10 題涵蓋職業、資金來源、付款人等須實際詢問客戶之事項；
# 自首次作答到送出短於此秒數，難以認定曾當面確認客戶身分。
MIN_INTERVIEW_SECONDS = 90


@dataclass
class CaseAnomaly:
    case_no: str
    assessment_id: int
    agent_name: str
    final_score: int | None
    final_level: str | None
    signals: list[str] = field(default_factory=list)

    @property
    def severity(self) -> int:
        return len(self.signals)


def _detail(event: AuditEvent) -> dict:
    if not event.detail:
        return {}
    try:
        return json.loads(event.detail)
    except json.JSONDecodeError:
        return {}


def analyze_case(events: list[AuditEvent], case: Assessment, threshold: int) -> list[str]:
    """對單一案件的作答軌跡產生訊號清單。"""
    signals: list[str] = []
    answer_events = [e for e in events if e.action == "assessment.answer"]
    if not answer_events:
        return signals

    revisions: dict[str, int] = defaultdict(int)
    peak_total = 0
    crossed_above = False
    downgrade_edits: list[str] = []

    for event in answer_events:
        d = _detail(event)
        total_after = d.get("total_after")
        if d.get("from") is not None:
            revisions[d.get("factor", "?")] += 1
        if isinstance(total_after, int):
            peak_total = max(peak_total, total_after)
            # 一旦總分曾達門檻，其後每一次調降都要記錄 —— 包含總分仍在門檻上的中間步驟。
            # 逐步走下來的軌跡（42→38→34→30→26）比只看最後一步更能說明意圖。
            if crossed_above and d.get("from") is not None:
                to_score = d.get("to_score")
                from_score = d.get("from_score")
                if (isinstance(to_score, int) and isinstance(from_score, int)
                        and to_score < from_score):
                    downgrade_edits.append(
                        f"{d.get('factor_label', d.get('factor'))}（{from_score}→{to_score} 分）"
                    )
            if total_after >= threshold:
                crossed_above = True

    final_general = case.risk_level == RiskLevel.GENERAL

    if final_general and crossed_above:
        signals.append(
            f"作答過程中總分曾達 {peak_total} 分（≥門檻 {threshold}），"
            "最終卻降至一般風險，請複核作答依據"
        )
    if final_general and downgrade_edits:
        signals.append("跨越門檻後曾調降下列因子：" + "、".join(dict.fromkeys(downgrade_edits)))
    if final_general and case.total_score is not None and \
            threshold - NEAR_THRESHOLD_MARGIN <= case.total_score < threshold:
        signals.append(f"最終總分 {case.total_score} 分緊貼門檻下緣（門檻 {threshold}）")

    heavy = [f"{f}（{n} 次）" for f, n in revisions.items() if n >= REVISION_ALERT]
    if heavy:
        signals.append("同一因子反覆改寫：" + "、".join(heavy))

    submitted = [e for e in events if e.action == "assessment.submit"]
    if submitted and answer_events:
        span = submitted[-1].at - answer_events[0].at
        if span < timedelta(seconds=MIN_INTERVIEW_SECONDS) and len(answer_events) >= 10:
            signals.append(
                f"自首次作答至送出僅 {int(span.total_seconds())} 秒，恐未實際詢問客戶"
            )
    return signals


def scan(db: Session, since: datetime | None = None, limit: int = 200) -> list[CaseAnomaly]:
    """掃描期間內已送出的案件，回傳有訊號者（依訊號數排序）。"""
    query = db.query(Assessment).filter(
        Assessment.status.in_(
            (
                AssessmentStatus.SUBMITTED,
                AssessmentStatus.PENDING_APPROVAL,
                AssessmentStatus.APPROVED,
                AssessmentStatus.REJECTED,
            )
        )
    )
    if since is not None:
        query = query.filter(Assessment.submitted_at >= since)
    cases = query.order_by(Assessment.submitted_at.desc()).limit(limit).all()
    if not cases:
        return []

    case_nos = [c.case_no for c in cases]
    events_by_case: dict[str, list[AuditEvent]] = defaultdict(list)
    for start in range(0, len(case_nos), 500):
        chunk = case_nos[start : start + 500]
        rows = (
            db.query(AuditEvent)
            .filter(AuditEvent.entity_type == "assessment", AuditEvent.entity_id.in_(chunk))
            .order_by(AuditEvent.at.asc(), AuditEvent.id.asc())
            .all()
        )
        for event in rows:
            events_by_case[event.entity_id].append(event)

    results: list[CaseAnomaly] = []
    for case in cases:
        from ..scoring.engine import load_questionnaire

        threshold = load_questionnaire(
            case.questionnaire_id, case.questionnaire_version
        ).high_risk_threshold
        signals = analyze_case(events_by_case.get(case.case_no, []), case, threshold)
        if signals:
            results.append(
                CaseAnomaly(
                    case_no=case.case_no,
                    assessment_id=case.id,
                    agent_name=case.agent.display_name if case.agent else "",
                    final_score=case.total_score,
                    final_level="高風險" if case.risk_level == RiskLevel.HIGH else "一般風險",
                    signals=signals,
                )
            )
    results.sort(key=lambda a: a.severity, reverse=True)
    return results
