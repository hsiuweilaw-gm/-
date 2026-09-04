from __future__ import annotations

import base64
import os
import tempfile

import pytest

# 測試以獨立的 SQLite 檔與固定金鑰執行，必須在匯入 app 之前設定。
_TMP = tempfile.mkdtemp(prefix="aml-test-")
os.environ["AML_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["AML_SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["AML_PII_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()
os.environ["AML_BOOTSTRAP_ADMIN_PASSWORD"] = ""

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Base, OrgUnit, Role, User  # noqa: E402
from app.security import hash_password  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
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
