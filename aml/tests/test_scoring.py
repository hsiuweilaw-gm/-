"""評分引擎測試。

每個測試都對應一條法規要求或紙本檢核表上的具體規則；
若日後有人調整分數或門檻而未同步修訂制度，測試必須失敗。
"""
from __future__ import annotations

import pytest

from app.scoring.engine import load_questionnaire, score_assessment


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def lowest(q) -> dict[str, str]:
    return {f.code: min(f.options, key=lambda o: o.score).code for f in q.factors}


def highest(q) -> dict[str, str]:
    return {f.code: max(f.options, key=lambda o: o.score).code for f in q.factors}


def test_questionnaire_matches_paper_form(q):
    """問卷須與現行紙本檢核表一致：10 個風險因子、10～48 分、門檻 30 分。"""
    assert len(q.factors) == 10
    assert (q.min_score, q.max_score) == (10, 48)
    assert q.high_risk_threshold == 30


def test_all_lowest_is_general_risk(q):
    """全選最低風險選項應為一般風險 — 這是絕大多數招攬案件的樣態。"""
    r = score_assessment(q, lowest(q))
    assert r.total_score == 10
    assert r.level == "general"
    assert r.level_label == "一般風險"
    assert not r.override_applied and not r.blocked


def test_all_highest_is_high_risk(q):
    r = score_assessment(q, highest(q))
    assert r.total_score == 48
    assert r.level == "high"


def test_threshold_boundary_is_inclusive(q):
    """紙本載明「30 分以上（含）者為高風險」，29 分仍為一般風險。"""
    base = lowest(q)
    # 由 10 分起，將產品風險改為 OIU（+4）、交易金額改為 500 萬以上（+4）…
    # 直接以人工構造分數驗證邊界，避免依賴特定選項組合。
    answers_29 = dict(base)
    answers_29["product_type"] = "oiu"          # 1 -> 5, 共 14
    answers_29["cust_amount"] = "over_5m"       # 1 -> 5, 共 18
    answers_29["txn_channel"] = "online"        # 1 -> 5, 共 22
    answers_29["txn_fund_source"] = "borrowed"  # 1 -> 5, 共 26
    answers_29["cust_entity_type"] = "legal_entity"  # 1 -> 3, 共 28
    r28 = score_assessment(q, answers_29)
    assert r28.total_score == 28 and r28.level == "general"

    answers_30 = dict(answers_29)
    answers_30["cust_source"] = "referral"      # 1 -> 2, 共 29
    assert score_assessment(q, answers_30).total_score == 29
    assert score_assessment(q, answers_30).level == "general"

    answers_30["cust_source"] = "cold_call"     # 1 -> 3, 共 30
    r30 = score_assessment(q, answers_30)
    assert r30.total_score == 30
    assert r30.level == "high", "30 分（含）以上應為高風險"


def test_sanction_list_hit_blocks_regardless_of_score(q):
    """範本第四點第八款：對象為制裁名單所列者，應婉拒建立業務關係。

    這正是現行純總分制的缺口 — 制裁名單命中只給 5 分，其餘皆最低分時
    總分僅 14 分，會被判為一般風險。系統必須擋件而非依總分放行。
    """
    r = score_assessment(q, lowest(q), refusal_checks={"sanction_list_hit"})
    assert r.total_score == 10, "總分本身不受強制規則影響，仍應如實呈現"
    assert r.blocked is True
    assert r.level == "high"
    assert any("婉拒" in reason for reason in r.blocked_reasons)


def test_ubo_unidentifiable_blocks(q):
    """問答集 Q7：客戶無法提供實質受益人資訊者，不得建立新業務關係。"""
    r = score_assessment(q, lowest(q), refusal_checks={"ubo_unidentifiable"})
    assert r.blocked is True


def test_pep_forces_high_risk_without_blocking(q):
    """PEP 應強制列為高風險並採強化措施，但非婉拒事由，不得擋件。"""
    r = score_assessment(q, lowest(q), mandatory_checks={"pep"})
    assert r.level == "high"
    assert r.override_applied is True
    assert r.blocked is False
    assert r.score_by_total_alone == "general", "純總分仍為一般風險，可證明等級係由強制規則提升"


def test_suspicious_pattern_forces_high_risk(q):
    """範本附錄態樣命中者，不論金額多寡均應列高風險並評估申報。"""
    r = score_assessment(q, lowest(q), suspicious_checks={"A2"})
    assert r.level == "high"
    assert any("疑似洗錢態樣" in reason for reason in r.override_reasons)


def test_sanctioned_geography_flag_forces_high_risk(q):
    """範本第五點第二款：來自高風險國家或地區者應採與其風險相當之強化措施。"""
    answers = dict(lowest(q))
    answers["geo_nationality"] = "sanctioned_country"
    r = score_assessment(q, answers)
    assert r.level == "high"
    assert "sanctioned_geography" in r.flags
    assert r.total_score < q.high_risk_threshold, "本案應由強制規則而非總分判定"


def test_incomplete_answers_are_reported(q):
    answers = lowest(q)
    answers.pop("product_type")
    r = score_assessment(q, answers)
    assert r.complete is False
    assert "product_type" in r.missing_factors


def test_category_scores_sum_to_total(q):
    r = score_assessment(q, highest(q))
    assert sum(c.score for c in r.category_scores) == r.total_score
