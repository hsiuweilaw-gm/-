"""一次性密碼測試。

自行實作 TOTP 的正當性，完全建立在「輸出與 RFC 6238 公布值相符」之上。
這些向量直接取自 RFC 6238 附錄 B，不是自己算出來再拿來對自己。
"""
from __future__ import annotations

import base64

from app import totp

# RFC 6238 附錄 B：SHA-1 的共用密鑰為 ASCII 字串 "12345678901234567890"
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

# （Unix 時間, 預期的 8 位數代碼）
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def test_matches_the_rfc_6238_published_test_vectors():
    for moment, expected in RFC_VECTORS:
        assert totp.code_at(RFC_SECRET, at=moment, digits=8) == expected, \
            f"t={moment} 與 RFC 6238 附錄 B 公布值不符"


def test_generated_secret_is_valid_base32_of_the_expected_length():
    secret = totp.generate_secret()
    raw = base64.b32decode(secret + "=" * (-len(secret) % 8))
    assert len(raw) == totp.SECRET_BYTES
    assert totp.generate_secret() != secret, "每次產生的密鑰必須不同"


def test_accepts_the_current_code_and_rejects_a_wrong_one():
    secret = totp.generate_secret()
    now = 1_700_000_000
    assert totp.verify(secret, totp.code_at(secret, at=now), at=now) is not None
    assert totp.verify(secret, "000000", at=now) is None


def test_tolerates_clock_drift_within_one_period_only():
    """手機與伺服器差幾秒是常態，差太多就不該放行。"""
    secret = totp.generate_secret()
    now = 1_700_000_000
    for drift in (-totp.PERIOD, 0, totp.PERIOD):
        code = totp.code_at(secret, at=now + drift)
        assert totp.verify(secret, code, at=now) is not None, f"誤差 {drift} 秒應可接受"
    for drift in (-2 * totp.PERIOD, 2 * totp.PERIOD):
        code = totp.code_at(secret, at=now + drift)
        assert totp.verify(secret, code, at=now) is None, f"誤差 {drift} 秒不應接受"


def test_a_used_code_cannot_be_replayed():
    """同一組代碼在有效期內只能用一次。

    少了這道，攻擊者肩窺或側錄到代碼後，仍有數十秒可以拿去登入。
    """
    secret = totp.generate_secret()
    now = 1_700_000_000
    code = totp.code_at(secret, at=now)

    counter = totp.verify(secret, code, at=now)
    assert counter is not None
    assert totp.verify(secret, code, at=now, last_counter=counter) is None, \
        "已使用過的代碼不得再次通過"


def test_rejects_malformed_input_without_raising():
    secret = totp.generate_secret()
    for bad in ("", "12345", "1234567", "abcdef", "12 34 56 78"):
        assert totp.verify(secret, bad, at=1_700_000_000) is None


def test_provisioning_uri_carries_the_parameters_apps_need():
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, "agent01", "示範保經")
    assert uri.startswith("otpauth://totp/")
    for expected in (f"secret={secret}", "digits=6", "period=30", "algorithm=SHA1"):
        assert expected in uri


def test_qr_renders_as_svg():
    uri = totp.provisioning_uri(totp.generate_secret(), "agent01", "示範保經")
    svg = totp.qr_svg(uri)
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
