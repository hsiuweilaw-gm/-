"""登入行為異常偵測測試。

每個訊號都要能抓到它該抓的形狀，也要在正常情形下保持安靜——
誤報一多，洗防人員就不看了，真正的異常跟著被埋掉。因此每個訊號
都成對測試：該響的時候響，不該響的時候不響。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models import AuditEvent, Role
from app.services import login_anomalies

from .conftest import make_user

TAIPEI = timezone(timedelta(hours=8))


@pytest.fixture
def agents(db):
    for i in range(1, 6):
        make_user(db, f"agent{i:02d}", Role.AGENT, None)
    return db


def log(db, action: str, username: str, ip: str, at: datetime) -> None:
    db.add(AuditEvent(action=action, entity_type="user", entity_id=username,
                      ip=ip, at=at))
    db.commit()


def kinds(signals) -> set[str]:
    return {s.kind for s in signals}


BASE = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)  # 台北 14:00，非深夜


def test_one_address_logging_in_many_agents_is_flagged(db, agents):
    """代填的典型形狀：同一台裝置十分鐘內逐一登入多個業務員帳號。"""
    for index in range(3):
        log(db, "auth.login", f"agent{index + 1:02d}", "203.0.113.5",
            BASE + timedelta(minutes=index * 2))

    signals = login_anomalies.scan(db, since=BASE - timedelta(days=1))
    assert "shared_address" in kinds(signals)
    flagged = next(s for s in signals if s.kind == "shared_address")
    assert len(flagged.subjects) == 3
    assert "代填" in flagged.detail


def test_colleagues_logging_in_across_the_day_are_not_flagged(db, agents):
    """同處辦公不是代填：各自在自己的時間登入，不會擠在十分鐘內。"""
    for index in range(5):
        log(db, "auth.login", f"agent{index + 1:02d}", "203.0.113.5",
            BASE + timedelta(hours=index))

    assert "shared_address" not in kinds(
        login_anomalies.scan(db, since=BASE - timedelta(days=1))
    )


def test_one_address_failing_against_many_accounts_is_flagged(db, agents):
    for index in range(3):
        log(db, "auth.login_failed", f"agent{index + 1:02d}", "198.51.100.7",
            BASE + timedelta(minutes=index))

    signals = login_anomalies.scan(db, since=BASE - timedelta(days=1))
    assert "probing" in kinds(signals)


def test_one_person_mistyping_their_own_password_is_not_probing(db, agents):
    """打錯自己的密碼很常見，不該當成攻擊。"""
    for index in range(4):
        log(db, "auth.login_failed", "agent01", "198.51.100.7",
            BASE + timedelta(minutes=index))

    assert "probing" not in kinds(
        login_anomalies.scan(db, since=BASE - timedelta(days=1))
    )


def test_repeated_failures_on_one_account_reach_the_lockout_signal(db, agents):
    for index in range(login_anomalies.FAILED_BURST):
        log(db, "auth.login_failed", "agent01", "198.51.100.7",
            BASE + timedelta(minutes=index))

    signals = login_anomalies.scan(db, since=BASE - timedelta(days=1))
    lockout = next(s for s in signals if s.kind == "lockout")
    assert "agent01" in lockout.subjects


def test_night_logins_are_judged_in_taipei_time(db, agents):
    """伺服器記錄的是世界標準時間，判定必須換算為台北時間。

    台北凌晨 3 點是世界標準時間前一日的 19 點——若直接以 UTC 的時數判斷，
    會把台北傍晚的正常登入標成深夜，而真正的深夜登入反而漏掉。
    """
    night_taipei = datetime(2026, 9, 2, 3, 30, tzinfo=TAIPEI)
    log(db, "auth.login", "agent01", "203.0.113.9", night_taipei.astimezone(UTC))

    signals = login_anomalies.scan(db, since=BASE - timedelta(days=1))
    night = next(s for s in signals if s.kind == "night")
    assert "03:30" in night.detail


def test_evening_work_is_not_treated_as_night(db, agents):
    """保險招攬在晚間是常態，晚上十點登入不是異常。"""
    evening_taipei = datetime(2026, 9, 2, 22, 0, tzinfo=TAIPEI)
    log(db, "auth.login", "agent01", "203.0.113.9", evening_taipei.astimezone(UTC))

    assert "night" not in kinds(
        login_anomalies.scan(db, since=BASE - timedelta(days=1))
    )


def test_quiet_when_nothing_unusual_happened(db, agents):
    log(db, "auth.login", "agent01", "203.0.113.9", BASE)
    log(db, "auth.login", "agent02", "203.0.113.10", BASE + timedelta(hours=3))
    assert login_anomalies.scan(db, since=BASE - timedelta(days=1)) == []


def test_most_serious_signals_come_first(db, agents):
    log(db, "auth.login", "agent01", "203.0.113.9",
        datetime(2026, 9, 2, 3, 0, tzinfo=TAIPEI).astimezone(UTC))
    for index in range(3):
        log(db, "auth.login", f"agent{index + 1:02d}", "203.0.113.5",
            BASE + timedelta(minutes=index))

    signals = login_anomalies.scan(db, since=BASE - timedelta(days=1))
    assert signals[0].kind == "shared_address", "嚴重的要排在前面，否則會被淹沒"
