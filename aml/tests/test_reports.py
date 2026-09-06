"""彙總與報表測試。

年度報表是要向主管機關申報的文件，數字算錯的後果由公司承擔，
因此件數、占比與加權平均都必須以人工可驗算的例子鎖住。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.scoring.engine import load_questionnaire
from app.services import aggregate, exporters
from app.services import assessments as svc


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def make_case(db, agent, q, overrides, premium=None):
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        option = overrides.get(factor.code) or min(factor.options, key=lambda o: o.score).code
        svc.save_answer(db, case, agent, factor.code, option)
    if premium is not None:
        svc.save_profile(db, case, agent, {"annual_premium": str(premium)})
    # 高風險案件須先照會主管才能送出（業務員看不到分數，由系統警示）
    if svc.evaluate(case).level == "high":
        svc.record_consultation(db, case, agent, "王經理")
    svc.submit(db, case, agent)
    return case


def test_empty_period_produces_zero_summary(db, agent):
    summary = aggregate.summarize(db, date(2026, 1, 1), date(2026, 12, 31))
    assert summary.total_cases == 0
    assert summary.high_risk_ratio == 0.0
    assert all(dim.average_score == 0.0 for dim in summary.dimensions.values())


def test_counts_and_shares_match_hand_calculation(db, agent, q):
    """3 件一般壽險（權重 2）+ 1 件 OIU（權重 5）。

    產品風險件數加權平均 = (2*3 + 5*1) / 4 = 2.75
    """
    for _ in range(3):
        make_case(db, agent, q, {"product_type": "life"}, premium=100_000)
    make_case(db, agent, q, {"product_type": "oiu"}, premium=100_000)

    today = date.today()
    summary = aggregate.summarize(db, today, today)
    product = summary.dimensions["product"]

    assert summary.total_cases == 4
    assert product.total_count == 4
    counts = {b.label: b.count for b in product.buckets}
    assert counts["一般壽險"] == 3 and counts["OIU"] == 1
    assert product.average_score == pytest.approx(2.75)

    life_bucket = next(b for b in product.buckets if b.label == "一般壽險")
    assert product.share(life_bucket) == pytest.approx(0.75)


def test_premium_weighting_differs_from_count_weighting(db, agent, q):
    """年度報表要求產品風險同時以件數與保費加權 —— 這兩者刻意會不同。

    3 件一般壽險各 10 萬（權重 2）+ 1 件 OIU 保費 900 萬（權重 5）：
      件數加權 = (2*3 + 5*1) / 4               = 2.75
      保費加權 = (2*300000 + 5*9000000) / 9300000 ≈ 4.90
    大額 OIU 單件即可把保費加權風險拉高，這正是保費加權的監理意義。
    """
    for _ in range(3):
        make_case(db, agent, q, {"product_type": "life"}, premium=100_000)
    make_case(db, agent, q, {"product_type": "oiu"}, premium=9_000_000)

    today = date.today()
    product = aggregate.summarize(db, today, today).dimensions["product"]
    assert product.average_score == pytest.approx(2.75)
    assert product.premium_weighted_score == pytest.approx(
        (2 * 300_000 + 5 * 9_000_000) / 9_300_000
    )
    assert product.premium_weighted_score > product.average_score


def test_drafts_are_excluded_from_reports(db, agent, q):
    """草稿不是招攬新契約，計入報表會使申報件數虛增。"""
    make_case(db, agent, q, {})
    svc.create_draft(db, agent)  # 未送出
    today = date.today()
    summary = aggregate.summarize(db, today, today)
    assert summary.total_cases == 1
    assert summary.draft_cases == 1


def test_blocked_and_high_risk_cases_are_counted_separately(db, agent, q):
    make_case(db, agent, q, {})  # 一般風險
    blocked = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, blocked, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.save_checks(db, blocked, agent, "refusal", ["sanction_list_hit"])
    svc.submit(db, blocked, agent)

    today = date.today()
    summary = aggregate.summarize(db, today, today)
    assert summary.total_cases == 2
    assert summary.blocked_cases == 1
    assert summary.high_risk_cases == 1
    assert summary.general_risk_cases == 1


def test_sanction_hits_counted_from_answers(db, agent, q):
    """年度報表「制裁名單」件數取自地域／國籍作答，非取自擋件勾選。"""
    make_case(db, agent, q, {"geo_nationality": "sanctioned_country"})
    today = date.today()
    assert aggregate.summarize(db, today, today).sanction_hits == 1


def test_annual_workbook_matches_regulator_layout(db, agent, q):
    """年度報表版面須與主管機關 115 年格式一致，欄列位置不可偏移。"""
    make_case(db, agent, q, {"product_type": "oiu"}, premium=5_000_000)
    today = date.today()
    summary = aggregate.summarize(db, today, today)
    ws = exporters.build_annual_workbook(summary, "測試保經公司").active

    assert ws["A1"].value == "招攬新契約總數"
    assert ws["B1"].value == 1
    # 左欄五個構面的標題位置
    assert ws["A5"].value == "地域風險"
    assert ws["A13"].value == "客戶風險 - 職業"
    assert ws["A21"].value == "客戶風險 - 自然人/非自然人"
    assert ws["A29"].value == "客戶風險 - 國籍"
    assert ws["A37"].value == "客戶風險 - 來源(與業務員如何認識)"
    # 右欄
    assert ws["G5"].value == "產品風險"
    assert ws["G17"].value == "交易風險"
    assert ws["G25"].value == "制裁名單、STR與境外電匯件數(含OIU保單)"
    assert ws["M10"].value == "新契約保費"
    # 桶別名稱與權重
    assert [ws[f"{c}6"].value for c in "BCDE"] == ["制裁名單", "國外", "直轄OR離島", "其它縣市"]
    assert [ws[f"{c}7"].value for c in "BCDE"] == [5, 3, 2, 1]
    assert [ws[f"{c}6"].value for c in "HIJKL"] == [
        "OIU", "投資/年金", "外幣/萬能利變", "一般壽險", "健康/傷害/旅平/產險"
    ]
    assert [ws[f"{c}7"].value for c in "HIJKL"] == [5, 4, 3, 2, 1]
    # 產品風險三個分數欄位
    assert ws["G10"].value == "件數風險分數"
    assert ws["G13"].value == "保費風險分數"
    assert ws["G14"].value == "產品風險平均分數"
    assert ws["H10"].value == 5.0 and ws["H13"].value == 5.0


def test_entity_columns_follow_regulator_order_not_form_order(db, agent, q):
    """紙本表單依風險高低列「非自然人(3)／自然人(1)」，
    主管機關年度報表則是「自然人／非自然人」。報表須照主管機關的欄序。"""
    make_case(db, agent, q, {})
    today = date.today()
    summary = aggregate.summarize(db, today, today)
    ws = exporters.build_annual_workbook(summary, "測試保經公司").active
    assert [ws["B22"].value, ws["C22"].value] == ["自然人", "非自然人"]
    assert [ws["B23"].value, ws["C23"].value] == [1, 3]
    # 表單本身仍維持紙本順序，不受影響
    factor = q.factor("cust_entity_type")
    assert [o.label for o in factor.options] == ["非自然人", "自然人"]


def test_annual_bucket_counts_sum_to_total(db, agent, q):
    """原表說明要求：各構面件數加總須與招攬新契約總數相等。"""
    for product in ("oiu", "life", "health_pa"):
        make_case(db, agent, q, {"product_type": product}, premium=200_000)
    today = date.today()
    summary = aggregate.summarize(db, today, today)
    for key, dim in summary.dimensions.items():
        assert dim.total_count == summary.total_cases, f"{key} 件數加總與總件數不符"


def test_board_workbook_lists_high_risk_and_blocked_cases(db, agent, org, q):
    make_case(db, agent, q, {})  # 一般風險，不應出現在明細
    blocked = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, blocked, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.save_profile(db, blocked, agent, {"holder_name": "王大明", "holder_id": "A123456789"})
    svc.save_checks(db, blocked, agent, "refusal", ["sanction_list_hit"])
    svc.submit(db, blocked, agent)

    today = date.today()
    summary = aggregate.summarize(db, today, today)
    cases = db.query(type(blocked)).all()
    wb = exporters.build_board_workbook(summary, cases, "測試保經公司", {org.id: org.name})

    detail = wb["高風險及擋件明細"]
    rows = [r for r in detail.iter_rows(min_row=2, values_only=True) if r[0]]
    assert len(rows) == 1, "僅高風險與擋件案件應列入明細"
    assert rows[0][0] == blocked.case_no
    assert rows[0][4] == "王○明", "董事會報告之個資須遮罩"
    assert rows[0][5] == "A12****789"
    assert "婉拒" in rows[0][11]
