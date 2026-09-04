"""名單比對測試。

名單維護得再勤，沒有真的拿去比對客戶就是形同虛設；
這些測試確保名單確實作用在每一次評估上。
"""
from __future__ import annotations

import pytest

from app.models import AssessmentStatus, RiskLevel
from app.scoring.engine import load_questionnaire
from app.services import assessments as svc
from app.services import audit, screening


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def fill_lowest(db, case, agent, q):
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)


def test_normalize_handles_width_case_and_spacing():
    """名單常混雜全形、大小寫與空白，比對前必須正規化，否則會漏比。"""
    assert screening.normalize("Ａｂ Ｃ") == screening.normalize("abc")
    assert screening.normalize(" 王 大 明 ") == "王大明"


def test_sanction_list_hit_blocks_the_case(db, agent, q):
    screening.upsert(db, "sanction", "示範制裁對象股份有限公司", "測試名單")
    db.commit()

    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent, {"holder_name": "示範制裁對象股份有限公司"})
    svc.submit(db, case, agent)

    assert case.status == AssessmentStatus.BLOCKED, "命中制裁名單應婉拒建立業務關係"
    assert case.total_score == 10, "擋件不改變總分"
    reasons = svc.evaluate_with_screening(db, case)[0].blocked_reasons
    assert any("制裁名單" in r and "完全相符" in r for r in reasons)


def test_pep_list_hit_forces_high_risk_without_blocking(db, agent, q):
    """PEP 名單命中應強制高風險走簽核，但不是婉拒事由。"""
    screening.upsert(db, "pep", "某政治人物", "測試名單")
    db.commit()

    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent, {"holder_name": "某政治人物"})
    svc.record_consultation(db, case, agent, "王經理")
    svc.submit(db, case, agent)

    assert case.risk_level == RiskLevel.HIGH
    assert case.status == AssessmentStatus.PENDING_APPROVAL
    assert case.override_applied is True


def test_beneficiary_is_screened_not_only_the_policyholder(db, agent, q):
    """範本第二點第八款要求辨識受益人；只比對要保人會放過人頭保單。"""
    screening.upsert(db, "sanction", "受制裁的受益人", "測試名單")
    db.commit()

    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent,
                     {"holder_name": "正常客戶", "beneficiary_name": "受制裁的受益人"})
    svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.BLOCKED


def test_no_hit_leaves_case_unaffected(db, agent, q):
    screening.upsert(db, "sanction", "完全不相干的名字", "測試名單")
    db.commit()

    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent, {"holder_name": "王大明"})
    svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.SUBMITTED
    assert case.risk_level == RiskLevel.GENERAL


def test_newly_added_list_entry_affects_existing_cases(db, agent, q):
    """名單是持續監控的工具：新增名單後，既有案件重新檢視時必須反映出來。"""
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent, {"holder_name": "後來被列名的人"})
    svc.submit(db, case, agent)
    assert svc.evaluate_with_screening(db, case)[0].blocked is False

    screening.upsert(db, "sanction", "後來被列名的人", "事後函轉之名單")
    db.commit()
    result, hits = svc.evaluate_with_screening(db, case)
    assert hits and result.blocked is True


def test_deactivated_entry_stops_matching(db, agent, q):
    entry = screening.upsert(db, "sanction", "誤列的名字", "測試名單")
    db.commit()
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "誤列的名字"})
    assert svc.evaluate_with_screening(db, case)[0].blocked is True

    entry.active = False
    db.commit()
    assert svc.evaluate_with_screening(db, case)[0].blocked is False


def test_watchlist_hit_is_recorded_in_audit_trail(db, agent, q):
    screening.upsert(db, "sanction", "示範制裁對象", "測試名單")
    db.commit()
    case = svc.create_draft(db, agent)
    fill_lowest(db, case, agent, q)
    svc.save_profile(db, case, agent, {"holder_name": "示範制裁對象"})
    svc.submit(db, case, agent)

    submits = [e for e in audit.trail(db, "assessment", case.case_no)
               if e.action == "assessment.submit"]
    hits = audit.parse_detail(submits[-1])["watchlist_hits"]
    assert hits and hits[0]["list"] == "sanction"
    assert hits[0]["field"] == "要保人"


def test_pasting_a_list_splits_only_on_real_newlines():
    """洗防人員常從主管機關函令的 PDF 直接貼上名單，可能夾帶 U+0085、U+2028 等字元。

    若以 str.splitlines() 切分，單一名稱會被切成兩筆殘缺資料，導致名單失效。
    """
    pasted = "甲公司\u0085乙丙公司\n丁公司"
    assert len(pasted.splitlines()) == 3, "splitlines 會在 U+0085 多切一刀"

    normalized = pasted.replace("\r\n", "\n").replace("\r", "\n")
    robust = [line.strip() for line in normalized.split("\n") if line.strip()]
    assert robust == ["甲公司\u0085乙丙公司", "丁公司"]


def test_watchlist_hit_is_recorded_permanently(db, agent, q):
    """業務員看到「應婉拒」後改寫姓名或放棄草稿，命中紀錄仍須留存。

    沒有這道留痕，只要放棄草稿另建新案，洗防人員永遠不會知道曾經命中過。
    """
    screening.add_entry(db, "sanction", "制裁對象公司", source="TW", external_id="T-1")
    db.commit()

    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "制裁對象公司"})
    assert case.watchlist_hit_at is not None
    assert "制裁對象公司" in case.watchlist_hit_note
    first_seen = case.watchlist_hit_at

    # 業務員改成不會命中的名字
    svc.save_profile(db, case, agent, {"holder_name": "正常客戶"})
    assert svc.evaluate_with_screening(db, case)[0].blocked is False, "改名後當下確實不再命中"
    assert case.watchlist_hit_at == first_seen, "但命中的事實必須留著"
    assert case.watchlist_hit_note


def test_case_with_no_hit_has_no_watchlist_mark(db, agent, q):
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "王大明"})
    assert case.watchlist_hit_at is None
