"""作答行為異常偵測測試。

系統向業務員揭露分數是既定需求，代價是「湊分數」的可能性。
這些測試確保第二道防線看得見湊分數的軌跡。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.scoring.engine import load_questionnaire
from app.services import anomalies, audit
from app.services import assessments as svc


def paced_trail(db, case_no: str, seconds_per_step: int = 30):
    """取出稽核軌跡，並把時間戳攤開成逐題詢問客戶的真實節奏。

    測試在毫秒內跑完，若不調整時間戳，每個案件都會誤觸「填答過快」訊號，
    使其他訊號的斷言失去意義。
    """
    events = audit.trail(db, "assessment", case_no)
    base = events[0].at
    for i, event in enumerate(events):
        event.at = base + timedelta(seconds=i * seconds_per_step)
    db.commit()
    return audit.trail(db, "assessment", case_no)


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def fill_lowest(db, case, agent, q, skip=()):
    for factor in q.factors:
        if factor.code in skip:
            continue
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)


def test_clean_case_produces_no_signal(db, agent, q):
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.submit(db, case, agent)
    events = paced_trail(db, case.case_no)
    assert anomalies.analyze_case(events, case, q.high_risk_threshold) == []


def test_crossing_threshold_then_dropping_back_is_flagged(db, agent, q):
    """業務員先如實作答達 30 分以上，再改答案降到門檻下 —— 必須被抓到。"""
    case = svc.create_draft(db, agent)
    high = {
        "geo_domicile": "overseas", "cust_occupation": "tier1", "cust_source": "inbound",
        "cust_amount": "over_5m", "product_type": "oiu", "txn_channel": "online",
        "txn_fund_source": "borrowed", "txn_payer": "third_party",
    }
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        high.get(factor.code) or min(factor.options, key=lambda o: o.score).code)
    assert svc.evaluate(case).total_score >= q.high_risk_threshold

    # 回頭把幾個高分項改成最低分，壓到門檻以下
    for code in ("product_type", "cust_amount", "txn_fund_source", "txn_channel",
                 "cust_occupation", "cust_source", "geo_domicile", "txn_payer"):
        factor = q.factor(code)
        svc.save_answer(db, case, agent, code, min(factor.options, key=lambda o: o.score).code)
    svc.submit(db, case, agent)

    signals = anomalies.analyze_case(paced_trail(db, case.case_no), case,
                                     q.high_risk_threshold)
    assert any("最終卻降至一般風險" in s for s in signals)
    downgrades = next(s for s in signals if "跨越門檻後曾調降" in s)
    # 逐步走下門檻的每一步都要列出，只記最後一步會低估情節。
    assert downgrades.count("→") >= 4, f"應列出多次調降，實際：{downgrades}"


def test_score_hugging_threshold_is_flagged(db, agent, q):
    """最終停在 27～29 分（門檻 30）本身就值得複核。"""
    case = svc.create_draft(db, agent)
    overrides = {
        "product_type": "oiu",          # 5
        "cust_amount": "over_5m",       # 5
        "txn_channel": "online",        # 5
        "txn_fund_source": "borrowed",  # 5
        "cust_entity_type": "legal_entity",  # 3
        "cust_source": "referral",      # 2
    }
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        overrides.get(factor.code)
                        or min(factor.options, key=lambda o: o.score).code)
    svc.submit(db, case, agent)
    assert case.total_score == 29

    signals = anomalies.analyze_case(paced_trail(db, case.case_no), case,
                                     q.high_risk_threshold)
    assert any("緊貼門檻下緣" in s for s in signals)


def test_repeated_revision_of_same_factor_is_flagged(db, agent, q):
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    for option in ("oiu", "ilp_annuity", "fx_ul", "health_pa"):
        svc.save_answer(db, case, agent, "product_type", option)
    svc.submit(db, case, agent)
    signals = anomalies.analyze_case(paced_trail(db, case.case_no), case,
                                     q.high_risk_threshold)
    assert any("反覆改寫" in s for s in signals)


def test_high_risk_outcome_is_not_flagged_for_downgrades(db, agent, q):
    """最終仍判高風險者沒有規避動機，不應produce降級訊號造成雜訊。"""
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_checks(db, case, agent, "mandatory", ["pep"])
    svc.save_answer(db, case, agent, "product_type", "oiu")
    svc.save_answer(db, case, agent, "product_type", "health_pa")
    svc.record_consultation(db, case, agent, "王經理")
    svc.submit(db, case, agent)
    signals = anomalies.analyze_case(paced_trail(db, case.case_no), case,
                                     q.high_risk_threshold)
    assert not any("降至一般風險" in s for s in signals)


def test_submitting_too_fast_is_flagged(db, agent, q):
    """10 題涵蓋職業、資金來源、付款人等須實際詢問之事項，
    自首次作答到送出不足 90 秒，難以認定曾當面確認客戶身分。"""
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.submit(db, case, agent)
    # 不呼叫 paced_trail：保留測試瞬間完成的原始時間戳，正是要驗證的情境
    signals = anomalies.analyze_case(audit.trail(db, "assessment", case.case_no), case,
                                     q.high_risk_threshold)
    assert any("恐未實際詢問客戶" in s for s in signals)


def test_scan_orders_by_signal_count(db, agent, q):
    quiet = svc.create_draft(db, agent)
    fill_lowest(db, quiet, agent, q)
    svc.submit(db, quiet, agent)
    paced_trail(db, quiet.case_no)

    noisy = svc.create_draft(db, agent)
    fill_lowest(db, noisy, agent, q)
    for option in ("oiu", "ilp_annuity", "fx_ul", "health_pa"):
        svc.save_answer(db, noisy, agent, "product_type", option)
    svc.submit(db, noisy, agent)
    paced_trail(db, noisy.case_no)

    results = anomalies.scan(db)
    assert [r.case_no for r in results] == [noisy.case_no], \
        "節奏正常且無其他訊號的案件不應出現在複核清單"
