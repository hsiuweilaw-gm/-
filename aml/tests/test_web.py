"""HTTP 層整合測試：登入、權限隔離、自動儲存 API、報表下載。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import AssessmentStatus, Role, User
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
    assert body["complete"] is False
    assert body["answered"] == 1 and body["total_factors"] == 10


def test_autosave_rejects_unknown_option(client, agent):
    login(client, "agent01")
    res = client.post("/assessments/new", follow_redirects=False)
    case_no = res.headers["location"].rsplit("/", 1)[1]
    res = client.post(f"/api/assessments/{case_no}/answer",
                      json={"factor": "product_type", "option": "not-a-real-option"})
    assert res.status_code == 422


def fill_high_risk(db, case, agent, q):
    high = {"geo_domicile": "overseas", "cust_occupation": "tier1", "cust_source": "inbound",
            "cust_amount": "over_5m", "product_type": "oiu", "txn_channel": "online",
            "txn_fund_source": "borrowed", "txn_payer": "third_party"}
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        high.get(factor.code) or min(factor.options, key=lambda o: o.score).code)


def test_agent_never_sees_score_or_risk_level(client, agent, q, db):
    """公司政策：業務員不得知悉客戶評分與風險等級，避免為規避強化盡職調查而調整作答。"""
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    fill_high_risk(db, case, agent, q)
    assert svc.evaluate(case).total_score >= 30

    res = client.get(f"/assessments/{case.case_no}")
    assert res.status_code == 200
    # 不得揭露本案的分數與等級
    assert "目前總分" not in res.text
    assert "風險等級" not in res.text
    assert "本案為高風險" not in res.text
    assert "門檻" not in res.text
    # 選項旁不得標示配分——業務員把 10 個數字加起來就還原了總分
    options_block = res.text.split("風險因子評分")[1].split("應加強確認客戶身分")[0]
    assert " 分</span>" not in options_block
    # 改以填答進度呈現，並在跨越門檻時警示照會主管
    assert "填答進度" in res.text
    assert "須照會單位主管確認後" in res.text


def test_agent_api_response_carries_no_score(client, agent, q, db):
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    res = client.post(f"/api/assessments/{case.case_no}/answer",
                      json={"factor": "product_type", "option": "oiu"})
    body = res.json()
    for leaked in ("total_score", "level", "level_label", "threshold", "category_scores",
                   "override_reasons"):
        assert leaked not in body, f"回應不得帶 {leaked}"
    assert body["answered"] == 1 and body["total_factors"] == 10


def test_supervisor_api_response_does_carry_score(client, db, agent, supervisor, q):
    """主管需要分數才能判斷是否同意，故對第一道防線督導以上揭露。"""
    case = svc.create_draft(db, agent)
    fill_high_risk(db, case, agent, q)
    login(client, "sup01")
    body = client.get(f"/api/assessments/{case.case_no}/status").json()
    assert body["total_score"] >= 30
    assert body["level"] == "high"


def test_agent_result_page_hides_score(client, agent, q, db):
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    client.post(f"/assessments/{case.case_no}/submit", follow_redirects=False)
    res = client.get(f"/assessments/{case.case_no}/result")
    assert res.status_code == 200
    assert "總分" not in res.text
    assert "各類別得分" not in res.text
    assert "評估已完成" in res.text


def test_high_risk_cannot_be_submitted_before_consulting_supervisor(client, agent, q, db):
    """業務員看不到分數，但跨越門檻時系統警示；須完成照會登錄才能送出。"""
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    fill_high_risk(db, case, agent, q)

    res = client.post(f"/assessments/{case.case_no}/submit")
    assert res.status_code == 400
    assert "須先照會單位主管" in res.text
    db.refresh(case)
    assert case.status == AssessmentStatus.DRAFT

    res = client.post(f"/api/assessments/{case.case_no}/consult",
                      json={"supervisor_name": "台北通訊處　王經理"})
    assert res.status_code == 200
    assert res.json()["consulted"] is True

    res = client.post(f"/assessments/{case.case_no}/submit", follow_redirects=False)
    assert res.status_code == 303
    db.refresh(case)
    assert case.status == AssessmentStatus.PENDING_APPROVAL
    assert case.consulted_name == "台北通訊處　王經理"
    assert case.consulted_at is not None


def test_general_risk_case_needs_no_consultation(client, agent, q, db):
    login(client, "agent01")
    case = svc.create_draft(db, agent)
    for factor in q.factors:
        svc.save_answer(db, case, agent, factor.code,
                        min(factor.options, key=lambda o: o.score).code)
    body = client.get(f"/api/assessments/{case.case_no}/status").json()
    assert body["needs_consultation"] is False
    res = client.post(f"/assessments/{case.case_no}/submit", follow_redirects=False)
    assert res.status_code == 303


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
    svc.record_consultation(db, case, agent, "王經理")
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
    svc.record_consultation(db, case, outsider, "高雄通訊處　李經理")
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


def test_roster_import_endpoint_creates_accounts(client, db):
    """管理者上傳 CSV 即可批次建帳號，並在畫面上取得初始密碼清單。"""
    from app.models import OrgUnit

    make_user(db, "admin01", Role.ADMIN)
    db.add(OrgUnit(code="TP01", name="台北通訊處"))
    db.commit()

    login(client, "admin01")
    content = (
        "帳號,姓名,角色,單位代碼\n"
        "agent101,王大明,agent,TP01\n"
        "agent102,李小華,agent,TP01\n"
    ).encode()
    res = client.post("/admin/roster", files={"file": ("roster.csv", content, "text/csv")})
    assert res.status_code == 200
    assert "新增 2 個帳號" in res.text
    assert "僅顯示這一次" in res.text
    assert db.query(User).filter(User.username == "agent101").one().display_name == "王大明"


def test_roster_import_rejects_bad_file_without_partial_writes(client, db):
    make_user(db, "admin01", Role.ADMIN)
    db.commit()
    before = db.query(User).count()

    login(client, "admin01")
    content = "帳號,姓名,角色,單位代碼\nagent101,王大明,agent,NOPE\n".encode()
    res = client.post("/admin/roster", files={"file": ("roster.csv", content, "text/csv")})
    assert res.status_code == 400
    assert "名冊未匯入" in res.text
    assert db.query(User).count() == before


def test_only_admin_can_import_roster(client, db, agent):
    login(client, "agent01")
    content = "帳號,姓名\nx,y\n".encode()
    res = client.post("/admin/roster", files={"file": ("roster.csv", content, "text/csv")})
    assert res.status_code == 403


def test_roster_template_is_downloadable(client, db):
    make_user(db, "admin01", Role.ADMIN)
    db.commit()
    login(client, "admin01")
    res = client.get("/admin/roster/template.csv")
    assert res.status_code == 200
    assert res.text.startswith("﻿"), "需帶 BOM，Excel 開啟中文才不會亂碼"
    assert "帳號" in res.text


def test_account_lockout_after_repeated_failures(client, db, agent):
    """連續登入失敗須鎖定帳號。

    此測試曾抓出一個時區缺陷：SQLite 取回的 locked_until 沒有時區，
    與 utcnow() 比較會拋 TypeError，導致鎖定判斷整個炸掉。
    """
    from app.deps import MAX_FAILED_LOGINS

    for _ in range(MAX_FAILED_LOGINS):
        client.post("/login", data={"username": "agent01", "password": "wrong"})
    res = client.post("/login", data={"username": "agent01", "password": PASSWORD})
    assert res.status_code == 423
    assert "鎖定" in res.text


def test_compliance_dashboard_lists_watchlist_hits(client, db, agent, compliance):
    """命中名單的案件必須出現在洗防儀表板，包含未送出的草稿。"""
    from app.services import screening

    screening.add_entry(db, "sanction", "制裁對象公司", source="TW", external_id="T-9")
    db.commit()
    case = svc.create_draft(db, agent)
    svc.save_profile(db, case, agent, {"holder_name": "制裁對象公司"})

    login(client, "aml01")
    res = client.get("/compliance")
    assert res.status_code == 200
    assert "曾命中制裁" in res.text
    assert case.case_no in res.text
