"""密碼雜湊、個資欄位加密與盲索引。

個資（姓名、身分證字號）依個資法及內控手冊 BIC06-02 要求加密儲存。
身分證字號另存 HMAC 盲索引，供「同一客戶歷次評估」查詢而無須解密全表。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
# scrypt 需要 128 * N * r bytes；OpenSSL 預設上限 32 MiB 剛好不足，須明確放寬。
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=128 * int(n) * int(r) * 2,
            dklen=len(dk_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


def _key() -> bytes:
    raw = base64.urlsafe_b64decode(get_settings().pii_key)
    if len(raw) != 32:
        raise ValueError("AML_PII_KEY 必須為 base64url 編碼的 32 bytes 金鑰")
    return raw


def encrypt_pii(plaintext: str | None) -> str | None:
    """以 AES-256-GCM 加密個資欄位。回傳 base64url(nonce||ciphertext)。"""
    if plaintext is None or plaintext == "":
        return None
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_pii(token: str | None) -> str | None:
    if not token:
        return None
    raw = base64.urlsafe_b64decode(token)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode()


def blind_index(value: str | None) -> str | None:
    """身分證字號的可查詢雜湊。大小寫與空白正規化後再取 HMAC。"""
    if not value:
        return None
    normalized = value.strip().upper().replace(" ", "")
    return hmac.new(_key(), normalized.encode(), hashlib.sha256).hexdigest()


def mask_id_number(value: str | None) -> str:
    """身分證字號遮罩顯示：A123456789 -> A12****789。"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 5:
        return v[0] + "*" * (len(v) - 1)
    return f"{v[:3]}{'*' * (len(v) - 6)}{v[-3:]}"


def mask_name(value: str | None) -> str:
    """姓名遮罩顯示：王小明 -> 王○明；John Smith -> J*** S****。"""
    if not value:
        return ""
    v = value.strip()
    if len(v) <= 1:
        return v
    if len(v) == 2:
        return v[0] + "○"
    return v[0] + "○" * (len(v) - 2) + v[-1]


def new_token() -> str:
    return secrets.token_urlsafe(32)
