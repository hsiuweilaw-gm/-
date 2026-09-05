"""既有客戶持續監督測試。

招攬當下評估一次不足以滿足範本第五點第一款第三目之「強化之持續監督」，
以及問答集 Q8 之定期檢視要求。這裡把兩件事鎖住：
名單事後新增時既有客戶要被抓出來，以及審查週期依風險等級排定。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models import AssessmentStatus, ReviewOutcome, RiskLevel, as_aware
from app.scoring.engine import load_questionnaire
from app.services import assessments as svc
from app.services import audit, reviews, screening


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def submit_case(db, agent, q, *, high=False, holder="王大明"):
    case = svc.create_draft(db, agent)
    overrides = {
        "geo_domicile": "overseas", "cust_occupation": "tier1", "cust_source": "inbound",
        "cust_amount": "over_5m", "product_type": "oiu", "txn_channel": "online",
        "txn_fund_source": "borrowed", "txn_payer": "third_party",
    } if high else {}
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        overrides.get(factor.code)
                        or min(factor.options, key=lambda o: o.score).code)
    svc.save_profile(db, case, agent, {"holder_name": holder})
    if svc.evaluate(case).level == "high":
        svc.record_consultation(db, case, agent, "王經理")
    svc.submit(db, case, agent)
    return case


# ------------------------------------------------------------ 重新篩檢

def test_customer_listed_after_the_fact_is_caught_by_rescreening(db, agent, compliance, q):
    """這是持續監督最主要的防護價值。

    名單每月更新，既有客戶可能在事後才被列入。沒有重新篩檢，只有在有人剛好
    重新開啟舊案件時才會發現——而沒有人會主動去開。
    """
    case = submit_case(db, agent, q, holder="後來被列名的公司")
    assert case.watchlist_hit_at is None

    screening.add_entry(db, "sanction", "後來被列名的公司", source="TW", external_id="T-1")
    db.commit()

    result = reviews.rescreen(db, actor=compliance)
    assert result.checked >= 1
    assert case in result.new_hits
    db.refresh(case)
    assert case.watchlist_hit_at is not None, "命中須留痕，才會出現在儀表板的命中清單"
    assert "後來被列名的公司" in case.watchlist_hit_note
    assert case.rescreened_at is not None


def test_rescreening_does_not_re_flag_cases_already_hit(db, agent, compliance, q):
    """先前已命中的案件早已在待辦清單上，重複標記只會製造雜訊。"""
    screening.add_entry(db, "sanction", "制裁對象", source="TW", external_id="T-2")
    db.commit()
    case = submit_case(db, agent, q, holder="制裁對象")
    first_hit = case.watchlist_hit_at
    assert first_hit is not None

    result = reviews.rescreen(db, actor=compliance)
    assert case not in result.new_hits
    db.refresh(case)
    assert as_aware(case.watchlist_hit_at) == as_aware(first_hit)


def test_rescreening_skips_closed_and_blocked_cases(db, agent, compliance, q):
    """已結案的業務關係已結束，擋件案件本就不得建立業務關係，都無須持續監督。"""
    blocked = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, blocked, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.save_checks(db, blocked, agent, "refusal", ["sanction_list_hit"])
    svc.submit(db, blocked, agent)
    assert blocked.status == AssessmentStatus.BLOCKED

    ok = submit_case(db, agent, q)
    result = reviews.rescreen(db, actor=compliance)
    assert result.checked == 1, "只應檢視仍在監督中的案件"
    assert ok.rescreened_at is not None
    assert blocked.rescreened_at is None


def test_rescreening_is_recorded_in_the_audit_trail(db, agent, compliance, q):
    submit_case(db, agent, q)
    reviews.rescreen(db, actor=compliance)
    events = [e for e in audit.trail(db, "watchlist", "rescreen")
              if e.action == "watchlist.rescreen"]
    assert events, "重新篩檢本身須留痕，才能證明有做"
    assert audit.parse_detail(events[-1])["checked"] >= 1


# ------------------------------------------------------------ 審查週期

def test_review_is_scheduled_on_submit_by_risk_level(db, agent, q):
    """高風險客戶須較頻繁複核（問答集 Q8：「特別是高風險客戶」）。"""
    general = submit_case(db, agent, q)
    high = submit_case(db, agent, q, high=True)

    assert general.review_due_on is not None
    assert high.review_due_on is not None
    assert high.review_due_on < general.review_due_on, "高風險的審查週期必須較短"

    from app.config import get_settings

    settings = get_settings()
    assert (general.review_due_on - date.today()).days > settings.review_months_high * 30


def test_blocked_case_gets_no_review_schedule(db, agent, q):
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.save_checks(db, case, agent, "refusal", ["sanction_list_hit"])
    svc.submit(db, case, agent)
    assert case.review_due_on is None, "擋件案件沒有業務往來可監督"


def test_due_cases_lists_overdue_first(db, agent, q):
    soon = submit_case(db, agent, q)
    later = submit_case(db, agent, q)
    soon.review_due_on = date.today() - timedelta(days=30)
    later.review_due_on = date.today()
    db.commit()

    due = reviews.due_cases(db)
    assert [c.case_no for c in due] == [soon.case_no, later.case_no]
    assert reviews.overdue_count(db) == 1


def test_recording_a_review_reschedules_and_keeps_history(db, agent, compliance, q):
    case = submit_case(db, agent, q)
    case.review_due_on = date.today() - timedelta(days=1)
    db.commit()

    review = reviews.record(db, case, compliance, ReviewOutcome.UNCHANGED, "已確認資料無異動")
    assert case.last_reviewed_on == date.today()
    assert case.review_due_on > date.today(), "審查後須排定下次應審查日"
    assert review.next_due_on == case.review_due_on
    assert case.reviews[0].note == "已確認資料無異動"
    assert reviews.overdue_count(db) == 0


def test_escalating_a_review_raises_the_risk_level(db, agent, compliance, q):
    case = submit_case(db, agent, q)
    assert case.risk_level == RiskLevel.GENERAL
    before_due = case.review_due_on

    reviews.record(db, case, compliance, ReviewOutcome.ESCALATED, "客戶職業變更")
    assert case.risk_level == RiskLevel.HIGH
    assert case.reviews[0].risk_level_before == RiskLevel.GENERAL
    assert case.reviews[0].risk_level_after == RiskLevel.HIGH
    assert case.review_due_on < before_due, "調升後審查週期須跟著縮短"


def test_terminating_closes_the_case_and_stops_monitoring(db, agent, compliance, q):
    case = submit_case(db, agent, q)
    reviews.record(db, case, compliance, ReviewOutcome.TERMINATED, "客戶已終止契約")
    assert case.status == AssessmentStatus.CLOSED
    assert case.review_due_on is None
    assert reviews.due_cases(db) == []


def test_review_is_recorded_in_the_audit_trail(db, agent, compliance, q):
    case = submit_case(db, agent, q)
    reviews.record(db, case, compliance, ReviewOutcome.DEESCALATED, "已補齊資金來源佐證")
    events = [e for e in audit.trail(db, "assessment", case.case_no)
              if e.action == "assessment.periodic_review"]
    assert len(events) == 1
    detail = audit.parse_detail(events[0])
    assert detail["outcome"] == "deescalated"
    assert detail["note"] == "已補齊資金來源佐證"
