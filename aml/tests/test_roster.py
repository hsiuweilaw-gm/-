"""名冊批次匯入測試。

300 人以上規模逐一建帳號不可行，此功能是實務上能否上線的關鍵。
匯入錯誤造成的後果（漏建、重設在職人員密碼、幽靈單位）都在此鎖住。
"""
from __future__ import annotations

import pytest

from app.models import OrgUnit, Role, User
from app.security import verify_password
from app.services import roster


@pytest.fixture
def units(db) -> dict[str, OrgUnit]:
    made = {}
    for code, name in (("TP01", "台北通訊處"), ("KH01", "高雄通訊處")):
        unit = OrgUnit(code=code, name=name)
        db.add(unit)
        made[code] = unit
    db.commit()
    return made


def csv_of(rows: list[list[str]]) -> str:
    header = "帳號,姓名,角色,單位代碼,業務員登錄字號,登錄有效日期,洗防訓練完訓日"
    return header + "\n" + "\n".join(",".join(r) for r in rows) + "\n"


def test_imports_agents_with_generated_passwords(db, units):
    content = csv_of([
        ["agent001", "王大明", "agent", "TP01", "經登字第123456號", "2027-06-30", "2026-03-15"],
        ["agent002", "李小華", "agent", "KH01", "", "", ""],
    ])
    result = roster.parse_and_import(db, content)
    db.commit()

    assert result.ok
    assert len(result.created) == 2
    assert {c.username for c in result.created} == {"agent001", "agent002"}

    user = db.query(User).filter(User.username == "agent001").one()
    assert user.display_name == "王大明"
    assert user.role == Role.AGENT
    assert user.org_unit_id == units["TP01"].id
    assert user.agent_license_no == "經登字第123456號"
    assert str(user.aml_training_date) == "2026-03-15"
    assert user.must_change_password is True, "首次登入必須強制變更密碼"

    credential = next(c for c in result.created if c.username == "agent001")
    assert verify_password(credential.password, user.password_hash), \
        "產出的初始密碼必須真的能登入，否則名冊等於沒匯"


def test_any_bad_row_aborts_the_whole_import(db, units):
    """匯到一半最難善後：不知道哪些已建、哪些沒建。故一律全有或全無。"""
    content = csv_of([
        ["agent001", "王大明", "agent", "TP01", "", "", ""],
        ["agent002", "李小華", "agent", "NOPE", "", "", ""],  # 單位代碼不存在
    ])
    result = roster.parse_and_import(db, content)
    db.commit()

    assert not result.ok
    assert any("NOPE" in e.message for e in result.errors)
    assert db.query(User).count() == 0, "有錯誤時整批都不得寫入"


def test_unknown_org_code_is_rejected_not_auto_created(db, units):
    """自動建立單位會讓打錯字變成幽靈單位，之後報表與權限都跟著錯。"""
    result = roster.parse_and_import(db, csv_of([["a1", "甲", "agent", "TP99", "", "", ""]]))
    assert not result.ok
    assert db.query(OrgUnit).count() == 2


def test_existing_account_is_updated_without_password_reset(db, units):
    """名冊會重複上傳做同步。若每次都重設密碼，會把在職人員鎖在系統外。"""
    roster.parse_and_import(db, csv_of([["agent001", "王大明", "agent", "TP01", "", "", ""]]))
    db.commit()
    original_hash = db.query(User).filter(User.username == "agent001").one().password_hash

    result = roster.parse_and_import(
        db, csv_of([["agent001", "王大明", "agent", "KH01", "經登字第999號", "", "2026-08-01"]])
    )
    db.commit()

    assert result.ok
    assert result.created == []
    assert result.updated == ["agent001"]
    user = db.query(User).filter(User.username == "agent001").one()
    assert user.password_hash == original_hash, "既有帳號不得因名冊同步被重設密碼"
    assert user.org_unit_id == units["KH01"].id
    assert user.agent_license_no == "經登字第999號"
    assert str(user.aml_training_date) == "2026-08-01"


def test_duplicate_username_in_file_is_rejected(db, units):
    result = roster.parse_and_import(db, csv_of([
        ["agent001", "王大明", "agent", "TP01", "", "", ""],
        ["agent001", "另一個人", "agent", "TP01", "", "", ""],
    ]))
    assert not result.ok
    assert any("重複" in e.message for e in result.errors)


def test_invalid_role_and_date_are_reported_with_line_numbers(db, units):
    result = roster.parse_and_import(db, csv_of([
        ["a1", "甲", "manager", "TP01", "", "", ""],
        ["a2", "乙", "agent", "TP01", "", "2027/06/30", ""],
    ]))
    assert not result.ok
    lines = {e.line for e in result.errors}
    assert lines == {2, 3}, "錯誤須標明列號，否則幾百列的檔案無從修正"


def test_missing_required_column_is_reported(db):
    result = roster.parse_and_import(db, "姓名,角色\n王大明,agent\n")
    assert not result.ok
    assert "帳號" in result.errors[0].message


def test_english_headers_and_bom_are_accepted(db, units):
    """Excel 另存的 CSV 會帶 BOM；有些公司名冊用英文欄名。兩者都要能吃。"""
    content = "﻿username,name,role,org_code\nagent001,王大明,agent,TP01\n"
    result = roster.parse_and_import(db, content)
    db.commit()
    assert result.ok
    assert db.query(User).filter(User.username == "agent001").one().display_name == "王大明"


def test_blank_lines_are_skipped(db, units):
    content = csv_of([["a1", "甲", "agent", "TP01", "", "", ""], ["", "", "", "", "", "", ""]])
    result = roster.parse_and_import(db, content)
    assert result.ok
    assert len(result.created) == 1


def test_role_defaults_to_agent(db, units):
    result = roster.parse_and_import(db, "帳號,姓名\nagent001,王大明\n")
    db.commit()
    assert result.ok
    assert db.query(User).filter(User.username == "agent001").one().role == Role.AGENT


def test_credentials_csv_contains_every_new_account(db, units):
    result = roster.parse_and_import(db, csv_of([
        ["a1", "甲", "agent", "TP01", "", "", ""],
        ["a2", "乙", "agent", "TP01", "", "", ""],
    ]))
    db.commit()
    output = roster.credentials_csv(result.created)
    assert output.count("\n") == 3  # 標題 + 2 列
    for c in result.created:
        assert c.password in output
