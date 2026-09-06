"""雙因素驗證的 HTTP 層測試。

系統存有客戶身分證字號。對外開放後，密碼是唯一關卡這件事本身就是缺口：
密碼會被釣魚、會被重複使用、會外流。這裡把第二道的每一個環節鎖住。

其他測試檔以 AML_TOTP_REQUIRED=false 執行；本檔逐項開啟，因此
get_settings 的快取須在每個測試前後清除。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ratelimit, totp
from app.config import get_settings
from app.db import get_db
from app.deps import PENDING_COOKIE, SESSION_COOKIE
from app.main import app
from app.models import Role, User, utcnow
from app.security import decrypt_pii, encrypt_pii

from .conftest import make_user

PASSWORD = "correct-horse-battery"


@pytest.fixture
def totp_on(monkeypatch):
    monkeypatch.setenv("AML_TOTP_REQUIRED", "true")
    get_settings.cache_clear()
    ratelimit.clear()
    yield
    get_settings.cache_clear()
    ratelimit.clear()


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def enrolled(db, username: str = "agent01", role: Role = Role.AGENT) -> tuple[User, str]:
    """已完成雙因素設定的帳號，連同其密鑰。"""
    user = make_user(db, username, role, None)
    secret = totp.generate_secret()
    user.totp_secret_enc = encrypt_pii(secret)
    user.totp_confirmed_at = utcnow()
    db.commit()
    return user, secret


def password_login(client, username: str = "agent01"):
    return client.post("/login", data={"username": username, "password": PASSWORD},
                       follow_redirects=False)


def test_password_alone_does_not_establish_a_session(client, db, totp_on):
    """密碼正確只換到一張效期五分鐘的中繼憑證，不是登入狀態。"""
    enrolled(db)
    res = password_login(client)

    assert res.status_code == 303
    assert res.headers["location"] == "/login/verify"
    assert SESSION_COOKIE not in res.cookies, "尚未通過第二道，不得核發工作階段"
    assert PENDING_COOKIE in res.cookies

    # 拿著中繼憑證直接闖受保護頁面也不行
    assert client.get("/assessments", follow_redirects=False).status_code == 401


def test_correct_code_completes_the_login(client, db, totp_on):
    _, secret = enrolled(db)
    password_login(client)

    res = client.post("/login/verify", data={"code": totp.code_at(secret)},
                      follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/assessments", follow_redirects=False).status_code == 200


def test_wrong_code_is_rejected_and_counts_towards_lockout(client, db, totp_on):
    user, _ = enrolled(db)
    password_login(client)

    res = client.post("/login/verify", data={"code": "000000"}, follow_redirects=False)
    assert res.status_code == 401
    db.refresh(user)
    assert user.failed_logins == 1, "一次性密碼錯誤與密碼錯誤同屬登入嘗試，須計入鎖定"
    assert client.get("/assessments", follow_redirects=False).status_code == 401


def test_a_code_cannot_be_used_twice(client, db, totp_on):
    """側錄或肩窺到的代碼，在其三十秒有效期內不得再用一次。"""
    user, secret = enrolled(db)
    code = totp.code_at(secret)

    password_login(client)
    assert client.post("/login/verify", data={"code": code},
                       follow_redirects=False).status_code == 303
    db.refresh(user)
    assert user.totp_last_counter is not None

    client.cookies.clear()
    password_login(client)
    res = client.post("/login/verify", data={"code": code}, follow_redirects=False)
    assert res.status_code == 401, "同一組代碼不得重放"


def test_verify_page_without_a_pending_credential_goes_back_to_login(client, db, totp_on):
    enrolled(db)
    assert client.get("/login/verify", follow_redirects=False).headers["location"] == "/login"
    res = client.post("/login/verify", data={"code": "123456"}, follow_redirects=False)
    assert res.headers["location"] == "/login"


def test_a_user_without_two_factor_cannot_reach_any_protected_page(client, db, totp_on):
    """必辦事項若只在首頁檢查，直接輸入其他網址就能略過。"""
    make_user(db, "agent01", Role.AGENT, None)
    res = password_login(client)
    assert res.status_code == 303

    for path in ("/", "/assessments", "/compliance", "/reports"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 303, f"{path} 應被導向"
        assert res.headers["location"] == "/totp/setup", f"{path} 未被導向設定頁"


def test_autosave_api_returns_401_rather_than_a_redirect(client, db, totp_on):
    """自動儲存是背景請求，收到 HTML 轉址只會顯示成莫名其妙的錯誤。"""
    make_user(db, "agent01", Role.AGENT, None)
    password_login(client)
    res = client.post("/api/assessments/AML-1/answer",
                      json={"factor": "geo_domicile", "option": "overseas"})
    assert res.status_code == 401


def test_enrolment_shows_a_qr_and_confirms_with_a_code(client, db, totp_on):
    user = make_user(db, "agent01", Role.AGENT, None)
    password_login(client)

    page = client.get("/totp/setup")
    assert page.status_code == 200
    assert "<svg" in page.text, "應提供 QR 供掃描"
    assert "otpauth://totp/" in page.text, "應提供可直接開啟 App 的連結"

    db.refresh(user)
    assert user.totp_secret_enc, "進入設定頁即產生密鑰"
    assert user.totp_confirmed_at is None, "尚未輸入驗證碼前不得視為已啟用"

    secret = decrypt_pii(user.totp_secret_enc)

    bad = client.post("/totp/setup", data={"code": "000000"}, follow_redirects=False)
    assert bad.status_code == 400
    db.refresh(user)
    assert user.totp_confirmed_at is None

    ok = client.post("/totp/setup", data={"code": totp.code_at(secret)},
                     follow_redirects=False)
    assert ok.status_code == 303
    db.refresh(user)
    assert user.totp_confirmed_at is not None
    assert client.get("/assessments", follow_redirects=False).status_code == 200


def test_the_secret_survives_a_page_refresh(client, db, totp_on):
    """每次重新整理都換一組密鑰的話，使用者剛掃完的 QR 就失效了。"""
    user = make_user(db, "agent01", Role.AGENT, None)
    password_login(client)
    client.get("/totp/setup")
    db.refresh(user)
    first = user.totp_secret_enc
    client.get("/totp/setup")
    db.refresh(user)
    assert user.totp_secret_enc == first


def test_admin_can_reset_a_lost_device(client, db, totp_on):
    """換手機或遺失裝置時的唯一救援途徑，且必須留痕。"""
    from app.services import audit

    target, _ = enrolled(db, "agent01", Role.AGENT)
    admin = make_user(db, "admin01", Role.ADMIN, None)
    admin.totp_secret_enc = encrypt_pii(totp.generate_secret())
    admin.totp_confirmed_at = utcnow()
    db.commit()
    admin_secret = decrypt_pii(admin.totp_secret_enc)

    password_login(client, "admin01")
    client.post("/login/verify", data={"code": totp.code_at(admin_secret)},
                follow_redirects=False)

    res = client.post(f"/admin/users/{target.id}/reset-totp", follow_redirects=False)
    assert res.status_code == 303
    db.refresh(target)
    assert target.totp_secret_enc is None and target.totp_confirmed_at is None

    actions = [e.action for e in audit.trail(db, "user", target.username)]
    assert "user.reset_totp" in actions


def test_repeated_attempts_from_one_address_are_throttled(client, db, totp_on):
    """帳號鎖定只擋單一帳號，擋不住換帳號輪流嘗試。"""
    make_user(db, "agent01", Role.AGENT, None)
    settings = get_settings()

    for i in range(settings.login_attempts_per_ip):
        client.post("/login", data={"username": f"nobody{i}", "password": "x"},
                    follow_redirects=False)

    res = client.post("/login", data={"username": "agent01", "password": PASSWORD},
                      follow_redirects=False)
    assert res.status_code == 429, "同一位置嘗試次數過多時應擋下，不論帳號是否存在"


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, peer: str, forwarded: str | None = None) -> None:
        self.client = _FakeClient(peer)
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}


def test_forwarded_for_is_read_from_the_right_not_the_left(monkeypatch):
    """X-Forwarded-For 的左半段由客戶端自行填寫，不可信。

    Nginx 的 $proxy_add_x_forwarded_for 是把真實來源接在後面。取最左邊
    那一段等於讓對方自報來源：稽核軌跡的位址會被偽造，來源位址限流
    也能靠輪流換偽造標頭完全繞過。
    """
    from app.routers.auth import client_ip

    monkeypatch.setenv("AML_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    forged = _FakeRequest("10.0.0.9", forwarded="8.8.8.8, 203.0.113.7")
    assert client_ip(forged) == "203.0.113.7", "應取代理接上的真實來源，而非客戶端自填值"

    assert client_ip(_FakeRequest("10.0.0.9", forwarded="203.0.113.7")) == "203.0.113.7"
    assert client_ip(_FakeRequest("10.0.0.9")) == "10.0.0.9", "無此標頭時以連線對端為準"
    get_settings.cache_clear()


def test_forwarded_for_is_ignored_when_there_is_no_proxy(monkeypatch):
    """直接對外時，任何人都能自訂此標頭，一律不採信。"""
    from app.routers.auth import client_ip

    monkeypatch.setenv("AML_TRUSTED_PROXY_HOPS", "0")
    get_settings.cache_clear()
    assert client_ip(_FakeRequest("203.0.113.9", forwarded="8.8.8.8")) == "203.0.113.9"
    get_settings.cache_clear()


def test_two_proxy_layers_are_supported(monkeypatch):
    from app.routers.auth import client_ip

    monkeypatch.setenv("AML_TRUSTED_PROXY_HOPS", "2")
    get_settings.cache_clear()
    request = _FakeRequest("10.0.0.9", forwarded="8.8.8.8, 203.0.113.7, 10.0.0.5")
    assert client_ip(request) == "203.0.113.7"
    # 標頭比預期短表示代理鏈與設定不符，退回連線對端而非猜測。
    assert client_ip(_FakeRequest("10.0.0.9", forwarded="203.0.113.7")) == "10.0.0.9"
    get_settings.cache_clear()


def test_privileged_roles_are_confined_to_the_allowlist(client, db, monkeypatch):
    """洗防、稽核、管理者看得到個資明文，其存取限於公司核准之位址。

    這是應用層的內鎖；反向代理是外牆。任何一層設定掉了，另一層還在，
    而且這一層擋下時會留下稽核軌跡，金融檢查時看得到。
    """
    from app.services import audit

    monkeypatch.setenv("AML_PRIVILEGED_IP_ALLOWLIST", "203.0.113.0/24")
    monkeypatch.setenv("AML_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    ratelimit.clear()

    make_user(db, "aml01", Role.COMPLIANCE, None)
    make_user(db, "agent01", Role.AGENT, None)

    password_login(client, "aml01")
    outside = {"x-forwarded-for": "8.8.8.8, 198.51.100.4"}
    assert client.get("/compliance", headers=outside,
                      follow_redirects=False).status_code == 403

    inside = {"x-forwarded-for": "8.8.8.8, 203.0.113.20"}
    assert client.get("/compliance", headers=inside,
                      follow_redirects=False).status_code == 200

    # 第一線不受此限：業務員本來就在外面招攬
    client.cookies.clear()
    password_login(client, "agent01")
    assert client.get("/assessments", headers=outside,
                      follow_redirects=False).status_code == 200

    actions = [e.action for e in audit.trail(db, "user", "aml01")]
    assert "auth.blocked_address" in actions, "擋下必須留痕——這是憑證外洩的重要徵候"

    get_settings.cache_clear()
    ratelimit.clear()


def test_an_empty_allowlist_restricts_nobody(client, db, monkeypatch):
    """試辦階段或尚未確定公司位址時，留空即不啟用。"""
    monkeypatch.setenv("AML_PRIVILEGED_IP_ALLOWLIST", "")
    get_settings.cache_clear()
    ratelimit.clear()

    make_user(db, "aml01", Role.COMPLIANCE, None)
    password_login(client, "aml01")
    assert client.get("/compliance", headers={"x-forwarded-for": "8.8.8.8, 198.51.100.4"},
                      follow_redirects=False).status_code == 200

    get_settings.cache_clear()
    ratelimit.clear()


def test_a_malformed_entry_does_not_silently_disable_the_whole_list(monkeypatch):
    """設定寫錯時寧可少一項而擋下，也不要整串失效而變成不設限。"""
    from app.net import ip_allowed

    allowlist = "203.0.113.0/24, 這不是位址, 198.51.100.7"
    assert ip_allowed("203.0.113.5", allowlist) is True
    assert ip_allowed("198.51.100.7", allowlist) is True
    assert ip_allowed("8.8.8.8", allowlist) is False
    assert ip_allowed(None, allowlist) is False
