"""時基一次性密碼（TOTP，RFC 6238）。

自行實作而非引入第三方套件：演算法本身僅二十餘行，且 RFC 6238 附錄 B
公布了完整的測試向量可逐一驗證。對金融檢查而言，「這段程式的每一個
輸出都與 RFC 公布值相符」比「我們用了某個套件」更容易說明，也少一個
需要持續追蹤漏洞的相依套件。

採用市面驗證器 App（Google Authenticator、Microsoft Authenticator 等）
的通行設定：HMAC-SHA1、30 秒週期、6 位數。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

DIGITS = 6
PERIOD = 30
# 允許前後各一個時間區間。手機與伺服器的時鐘難免有數秒誤差，
# 完全不容忍會造成大量「明明輸入正確卻失敗」的客訴。
WINDOW = 1
SECRET_BYTES = 20  # RFC 4226 建議之 HMAC-SHA1 金鑰長度


def generate_secret() -> str:
    """產生新的共用密鑰，以 base32 表示（驗證器 App 的通用格式）。"""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def format_secret(secret: str) -> str:
    """分組顯示，供無法掃描 QR 時人工輸入。"""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def _counter_code(secret: str, counter: int, digits: int = DIGITS,
                  algorithm: str = "sha1") -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algorithm)).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def code_at(secret: str, at: float | None = None, digits: int = DIGITS,
            algorithm: str = "sha1", period: int = PERIOD) -> str:
    """指定時刻應顯示的代碼。測試用；正式流程只驗證不產生。"""
    moment = time.time() if at is None else at
    return _counter_code(secret, int(moment // period), digits, algorithm)


def verify(secret: str, code: str, *, at: float | None = None,
           window: int = WINDOW, last_counter: int | None = None) -> int | None:
    """驗證代碼，回傳所命中的時間區間；不符則回傳 None。

    回傳區間編號是為了防止重放：呼叫端須記錄已使用的區間，
    同一組代碼在其 30 秒有效期內只能用一次。少了這道，攻擊者
    在肩窺或側錄到代碼後，仍有數十秒可以拿去登入。
    """
    cleaned = "".join(ch for ch in code if ch.isdigit())
    if len(cleaned) != DIGITS:
        return None
    moment = time.time() if at is None else at
    current = int(moment // PERIOD)
    for offset in range(-window, window + 1):
        counter = current + offset
        if last_counter is not None and counter <= last_counter:
            continue  # 已用過的區間一律拒絕
        if hmac.compare_digest(_counter_code(secret, counter), cleaned):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """otpauth:// URI，供產生 QR 或在手機上直接點開驗證器 App。"""
    label = urllib.parse.quote(f"{issuer}:{account}")
    params = urllib.parse.urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": DIGITS,
        "period": PERIOD,
    })
    return f"otpauth://totp/{label}?{params}"


def qr_svg(uri: str) -> str:
    """把 otpauth URI 畫成 QR 的 SVG 內容，直接內嵌於頁面。"""
    import io

    import qrcode
    import qrcode.image.svg

    image = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()
