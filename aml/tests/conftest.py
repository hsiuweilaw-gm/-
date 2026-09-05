from __future__ import annotations

import base64
import os
import tempfile

import pytest

# 測試以獨立的 SQLite 檔與固定金鑰執行，必須在匯入 app 之前設定。
# 預設以 SQLite 執行（快）。設定 AML_TEST_DATABASE_URL 可改指向 PostgreSQL，
# 用來抓出兩者的行為差異（時區、型別、約束）——這類差異只在正式環境才會爆炸。
# 空字串視同未設定：CI 的矩陣寫法在 SQLite 那一輪會把此變數設為空字串。
_TMP = tempfile.mkdtemp(prefix="aml-test-")
os.environ["AML_DATABASE_URL"] = (
    os.environ.get("AML_TEST_DATABASE_URL") or f"sqlite:///{_TMP}/test.db"
)
os.environ["AML_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["AML_PII_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()
os.environ["AML_BOOTSTRAP_ADMIN_PASSWORD"] = ""
# 雙因素驗證在測試環境預設關閉，讓各項功能測試專注於自己的標的；
# 開啟後的強制設定、登入第二道、重放防護等行為由 test_auth_2fa.py 涵蓋。
os.environ["AML_TOTP_REQUIRED"] = "false"

from app.db import SessionLocal, engine, stamp_head  # noqa: E402
from app.models import Base, OrgUnit, Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # 補上版本標記，否則應用程式的啟動檢查會擋下測試用資料庫。
    # 遷移本身與模型是否一致，由 tests/test_migrations.py 驗證。
    stamp_head()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_user(db, username: str, role: Role, org_unit_id: int | None = None) -> User:
    user = User(
        username=username,
        display_name=username,
        password_hash=hash_password("correct-horse-battery"),
        role=role,
        org_unit_id=org_unit_id,
        must_change_password=False,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def org(db) -> OrgUnit:
    unit = OrgUnit(code="TP01", name="台北通訊處")
    db.add(unit)
    db.commit()
    return unit


@pytest.fixture
def agent(db, org) -> User:
    return make_user(db, "agent01", Role.AGENT, org.id)


@pytest.fixture
def supervisor(db, org) -> User:
    return make_user(db, "sup01", Role.SUPERVISOR, org.id)


@pytest.fixture
def compliance(db) -> User:
    return make_user(db, "aml01", Role.COMPLIANCE)
