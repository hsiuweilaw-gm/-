"""HTTP 層整合測試：登入、權限隔離、自動儲存 API、報表下載。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import AssessmentStatus, Role
from app.scoring.engine import load_questionnaire
from app.services import assessments as svc

from .conftest import make_user

PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, username: str) -> TestClient:
    res = client.post("/login", data={"username": username, "password": PASSWORD},
                      follow_redirects=False)
    assert res.status_code == 303, res.text
    return client


@pytest.fixture
def q():
    return load_questionnaire("life", 1)


def test_login_rejects_bad_password(client, agent):
    res = client.post("/login", data={"username": "agent01", "password": "wrong"},
                      follow_redirects=False)
    assert res.status_code == 401
    assert "帳號或密碼錯誤" in res.text


def test_login_does_not_reveal_whether_account_exists(client, agent):
    missing = client.post("/login", data={"username": "nobody", "password": "wrong"})
    wrong = client.post("/login", data={"username": "agent01", "password": "wrong"})
    assert "帳號或密碼錯誤" in missing.text and "帳號或密碼錯誤" in wrong.text


def test_anonymous_is_redirected_to_login(client):
    res = client.get("/assessments", follow_redirects=False)
    assert res.status_code == 401


def test_agent_can_create_and_autosave(client, agent, q):
    login(client, "agent01")
    res = client.post("/assessments/new", follow_redirects=False)
    case_no = res.headers["location"].rsplit("/", 1)[1]

    res = client.post(f"/api/assessments/{case_no}/answer",
                      json={"factor": "product_type", "option": "oiu"})
    assert res.status_code == 200
    body = res.json()
    assert body["total_score"] == 5
    assert body["complete"] is False
    assert body["answered"] == 1 and body["total_factors"] == 10


def test_autosave_rejects_unknown_option(client, agent):
    login(client, "agent01")
    res = client.post("/assessments/new", follow_redirects=False)
    case_no = res.headers["location"].rsplit("/", 1)[1]
    res = client.post(f"/api/assessments/{case_no}/answer",
                      json={"factor": "product_type", "option": "not-a-real-option"})
    assert res.status_code == 422


def test_agent_sees_score_and_level_on_the_form(client, agent, q, db):
    """需求明訂：業務員必須看得到客戶評分。"""
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        max(factor.options, key=lambda o: o.score).code)
    res = client.get(f"/assessments/{case.case_no}")
    assert res.status_code == 200
    assert "48" in res.text, "總分應顯示於畫面"
    assert "高風險" in res.text


def test_blocked_case_shows_refusal_notice_to_agent(client, agent, q, db):
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.save_checks(db, case, agent, "refusal", ["sanction_list_hit"])
    res = client.get(f"/assessments/{case.case_no}")
    assert "應婉拒建立業務關係" in res.text


def test_agent_list_shows_holder_name_unmasked_for_own_cases(client, db, agent):
    """業務員檢視自己承辦的客戶，姓名不遮罩 —— 遮罩了反而無從辨認案件。"""
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "王大明"})
    res = client.get("/assessments")
    assert "王大明" in res.text


def test_agent_cannot_view_another_agents_case(client, db, agent, org, q):
    other = make_user(db, "agent02", Role.AGENT, org.id)
    case = svc.create_draft(db, other)
    login(client, "agent01")
    res = client.get(f"/assessments/{case.case_no}")
    assert res.status_code == 403


def test_agent_cannot_reach_compliance_console(client, agent):
    login(client, "agent01")
    assert client.get("/compliance").status_code == 403
    assert client.get("/reports").status_code == 403


def test_submitted_case_is_locked_against_further_edits(client, db, agent, q):
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    res = client.post(f"/assessments/{case.case_no}/submit", follow_redirects=False)
    assert res.status_code == 303
    res = client.post(f"/api/assessments/{case.case_no}/answer",
                      json={"factor": "product_type", "option": "oiu"})
    assert res.status_code == 409, "送出後不得再修改作答"


def test_supervisor_approval_requires_fund_source(client, db, agent, supervisor, q):
    case = svc.create_draft(db, agent)
    high = {"geo_domicile": "overseas", "cust_occupation": "tier1", "cust_source": "inbound",
            "cust_amount": "over_5m", "product_type": "oiu", "txn_channel": "online",
            "txn_fund_source": "borrowed", "txn_payer": "third_party"}
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        high.get(factor.code) or min(factor.options, key=lambda o: o.score).code)
    svc.submit(db, case, agent)
    assert case.status == AssessmentStatus.PENDING_APPROVAL

    login(client, "sup01")
    res = client.post(f"/review/{case.case_no}/decision",
                      data={"decision": "approved", "comment": "已面談"})
    assert res.status_code == 400
    assert "財富來源" in res.text
    db.refresh(case)
    assert case.status == AssessmentStatus.PENDING_APPROVAL, "資金來源留白不得放行"

    res = client.post(f"/review/{case.case_no}/decision",
                      data={"decision": "approved", "comment": "已面談",
                            "wealth_source": "自營事業盈餘",
                            "fund_source_detail": "近三年營業收入，已取具財報"},
                      follow_redirects=False)
    assert res.status_code == 303
    db.refresh(case)
    assert case.status == AssessmentStatus.APPROVED


def test_supervisor_only_sees_own_org_unit(client, db, agent, supervisor, org, q):
    from app.models import OrgUnit

    other_unit = OrgUnit(code="KH01", name="高雄通訊處")
    db.add(other_unit)
    db.commit()
    outsider = make_user(db, "agent99", Role.AGENT, other_unit.id)
    case = svc.create_draft(db, outsider)
    for factor in q.factors:
        svc.save_answer(db, case, outsider, factor.code,
                        max(factor.options, key=lambda o: o.score).code)
    svc.submit(db, case, outsider)

    login(client, "sup01")
    res = client.get("/review")
    assert case.case_no not in res.text, "主管不得看到其他通訊處的案件"
    assert client.get(f"/review/{case.case_no}").status_code == 403


def test_compliance_can_export_reports(client, db, agent, compliance, q):
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    svc.submit(db, case, agent)

    login(client, "aml01")
    from datetime import date

    today = date.today().isoformat()
    board = client.get("/reports/board.xlsx", params={"start": today, "end": today})
    assert board.status_code == 200
    assert board.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    )
    annual = client.get("/reports/annual.xlsx",
                        params={"roc_year": date.today().year - 1911})
    assert annual.status_code == 200
    assert len(annual.content) > 4000


def test_report_exports_are_logged(client, db, agent, compliance):
    from datetime import date

    from app.models import ReportExport

    login(client, "aml01")
    today = date.today().isoformat()
    client.get("/reports/board.xlsx", params={"start": today, "end": today})
    logs = db.query(ReportExport).all()
    assert len(logs) == 1
    assert logs[0].report_type == "board"
    assert logs[0].checksum, "須留存內容雜湊值以供金檢佐證"


def test_auditor_is_read_only(client, db, agent, q):
    make_user(db, "audit01", Role.AUDITOR)
    login(client, "audit01")
    assert client.get("/compliance").status_code == 200
    assert client.get("/compliance/cases").status_code == 200
    # 稽核不得維護名單，亦不得簽核
    res = client.post("/compliance/watchlist",
                      data={"list_type": "sanction", "values": "測試"})
    assert res.status_code == 403


def test_healthz_is_public(client):
    assert client.get("/healthz").json()["status"] == "ok"
