"""案件流程測試：建立 → 自動儲存 → 送出 → 簽核。"""
from __future__ import annotations

import pytest

from app.models import AssessmentStatus, RiskLevel
from app.scoring.engine import load_questionnaire
from app.security import decrypt_pii
from app.services import assessments as svc
from app.services import audit


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def answer_all(db, case, agent, q, overrides=None):
    overrides = overrides or {}
    for factor in q.factors:
        option = overrides.get(factor.code) or min(factor.options, key=lambda o: o.score).code
        svc.save_answer(db, case, agent, factor.code, option)
    return svc.evaluate(case)


def test_draft_is_created_with_sequential_case_no(db, agent):
    first = svc.create_draft(db, agent)
    second = svc.create_draft(db, agent)
    assert first.status == AssessmentStatus.DRAFT
    assert first.case_no.startswith("AML-")
    assert int(second.case_no.rsplit("-", 1)[1]) == int(first.case_no.rsplit("-", 1)[1]) + 1


def test_every_answer_is_persisted_immediately(db, agent, q):
    """自動儲存的核心保證：每一次作答都已落庫，關掉頁面不會遺失。"""
    case = svc.create_draft(db, agent)
    svc.save_answer(db, case, agent, "product_type", "oiu")
    db.expire_all()
    reloaded = db.get(type(case), case.id)
    assert svc.answers_map(reloaded) == {"product_type": "oiu"}
    assert reloaded.total_score == 5


def test_answer_change_is_recorded_with_before_and_after(db, agent):
    case = svc.create_draft(db, agent)
    svc.save_answer(db, case, agent, "product_type", "oiu")
    svc.save_answer(db, case, agent, "product_type", "health_pa")
    events = [e for e in audit.trail(db, "assessment", case.case_no)
              if e.action == "assessment.answer"]
    assert len(events) == 2, "改答案是新增一筆軌跡，不是覆蓋原紀錄"
    second = audit.parse_detail(events[1])
    assert second["from"] == "oiu" and second["from_score"] == 5
    assert second["to"] == "health_pa" and second["to_score"] == 1


def test_incomplete_submission_is_rejected(db, agent, q):
    case = svc.create_draft(db, agent)
    svc.save_answer(db, case, agent, "product_type", "oiu")
    with pytest.raises(ValueError, match="未填答"):
        svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.DRAFT


def test_general_risk_case_completes_without_approval(db, agent, q):
    case = svc.create_draft(db, agent)
    answer_all(db, case, agent, q)
    result = svc.submit(db, case, agent)
    assert result.level == "general"
    assert case.status == AssessmentStatus.SUBMITTED
    assert case.risk_level == RiskLevel.GENERAL
    assert case.submitted_at is not None
    assert case.retain_until is not None, "須設定紀錄保存期限（範本第六點）"


def test_high_risk_case_requires_consultation_then_approval(db, agent, q):
    """業務員看不到分數，但跨越門檻時系統警示；須先照會主管才能送出，再經主管同意。"""
    case = svc.create_draft(db, agent)
    answer_all(db, case, agent, q, overrides={
        "geo_domicile": "overseas", "cust_occupation": "tier1", "cust_source": "inbound",
        "cust_amount": "over_5m", "product_type": "oiu", "txn_channel": "online",
        "txn_fund_source": "borrowed", "txn_payer": "third_party",
    })
    with pytest.raises(ValueError, match="照會單位主管"):
        svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.DRAFT

    svc.record_consultation(db, case, agent, "王經理")
    result = svc.submit(db, case, agent)
    assert result.total_score >= 30
    assert case.status == AssessmentStatus.PENDING_APPROVAL, \
        "高風險案件不得逕行完成，須經主管同意（範本第五點第一款第一目）"
    assert case.consulted_name == "王經理"


def test_sanction_hit_blocks_the_case(db, agent, q):
    case = svc.create_draft(db, agent)
    answer_all(db, case, agent, q)
    svc.save_checks(db, case, agent, "refusal", ["sanction_list_hit"])
    svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.BLOCKED
    assert case.risk_level == RiskLevel.HIGH
    assert case.total_score == 10, "擋件不改變總分；總分仍應如實記載於稽核軌跡"


def test_pep_case_is_high_risk_even_at_minimum_score(db, agent, q):
    case = svc.create_draft(db, agent)
    answer_all(db, case, agent, q)
    svc.save_checks(db, case, agent, "mandatory", ["pep"])
    svc.record_consultation(db, case, agent, "王經理")
    svc.submit(db, case, agent)
    assert case.total_score == 10
    assert case.risk_level == RiskLevel.HIGH
    assert case.status == AssessmentStatus.PENDING_APPROVAL
    assert case.override_applied is True


def test_pii_is_encrypted_at_rest_and_recoverable(db, agent):
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "王大明", "holder_id": "A123456789"})
    assert "王大明" not in (case.holder_name_enc or "")
    assert "A123456789" not in (case.holder_id_enc or "")
    assert decrypt_pii(case.holder_name_enc) == "王大明"
    assert decrypt_pii(case.holder_id_enc) == "A123456789"
    assert case.holder_id_bidx, "須產生盲索引以供查詢同一客戶歷次評估"


def test_profile_audit_does_not_leak_plaintext_pii(db, agent):
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "王大明", "holder_id": "A123456789"})
    for event in audit.trail(db, "assessment", case.case_no):
        assert "王大明" not in (event.detail or "")
        assert "A123456789" not in (event.detail or "")


def test_offshore_remittance_flag_is_derived_from_answers(db, agent, q):
    """年度報表需獨立統計境外電匯／OIU 件數，故此旗標須由作答自動推導。"""
    case = svc.create_draft(db, agent)
    answer_all(db, case, agent, q, overrides={"txn_payer": "oiu_wire"})
    svc.submit(db, case, agent)
    assert case.offshore_remittance is True
