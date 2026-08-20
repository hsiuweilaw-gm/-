# -*- coding: utf-8 -*-
"""遮罩策略。

三種模式：
  partial —— 部分遮罩（預設）。保留頭尾少量字元供比對用，例：A12****789、0912***678
  full    —— 全遮罩。整段以 * 取代，長度不變
  label   —— 標籤取代。以〔類型〕取代，例：[身分證字號]
"""
from __future__ import annotations

import re
from typing import Callable, Dict

from .detectors import PIIMatch, _SURNAMES_COMPOUND, _CITY, _DIST

MASK_CHAR = "*"
NAME_MASK_CHAR = "○"

MODES = ("partial", "full", "label")


def mask_keep(text: str, head: int, tail: int, ch: str = MASK_CHAR) -> str:
    """保留前 head、後 tail 個字元，其餘以 ch 取代。太短則全遮。"""
    if len(text) <= head + tail:
        return ch * len(text)
    return text[:head] + ch * (len(text) - head - tail) + text[len(text) - tail:]


def mask_digits_keep(text: str, head: int, tail: int, ch: str = MASK_CHAR) -> str:
    """只遮數字：保留前 head、後 tail 個「數字」，其餘數字以 ch 取代，
    非數字字元（-、空白、括號）原樣保留，維持原有格式。"""
    positions = [i for i, c in enumerate(text) if c.isdigit()]
    n = len(positions)
    if n <= head + tail:
        to_mask = positions
    else:
        to_mask = positions[head:n - tail]
    chars = list(text)
    for i in to_mask:
        chars[i] = ch
    return "".join(chars)


def _mask_name(text: str) -> str:
    keep = 2 if any(text.startswith(s) for s in _SURNAMES_COMPOUND) else 1
    if len(text) <= keep:
        return NAME_MASK_CHAR * len(text)
    return text[:keep] + NAME_MASK_CHAR * (len(text) - keep)


# 縣市可有可無（「板橋區…」這類未寫縣市的地址保留到行政區層級即可）
_ADDR_PREFIX_RE = re.compile("^(?:" + _CITY + ")?" + _DIST)


def _mask_address(text: str) -> str:
    """保留到鄉鎮市區層級（去識別化常規做法），其餘遮罩。

    地址中沒有縣市／鄉鎮市區前綴時（門牌片段如「745號」「文化路9號」），
    整段遮罩——保留開頭反而會洩漏門牌或路名。"""
    m = _ADDR_PREFIX_RE.match(text)
    if m and 0 < m.end() < len(text):
        return m.group(0) + MASK_CHAR * 3
    return MASK_CHAR * len(text)


def _mask_email(text: str) -> str:
    local, _, domain = text.partition("@")
    keep = 2 if len(local) > 3 else 1
    return local[:keep] + MASK_CHAR * 3 + "@" + domain


def _mask_date(text: str) -> str:
    return re.sub(r"[0-9０-９]", MASK_CHAR, text)


# 各類型的部分遮罩規則
_PARTIAL_MASKERS: Dict[str, Callable[[str], str]] = {
    "national_id": lambda t: mask_keep(t, 3, 3),          # A12****789
    "arc_id": lambda t: mask_keep(t, 3, 3),
    "credit_card": lambda t: mask_digits_keep(t, 0, 4),   # **** **** **** 1234
    "nhi_card": lambda t: mask_digits_keep(t, 4, 2),
    "passport": lambda t: mask_digits_keep(t, 0, 3),
    "ubn": lambda t: mask_keep(t, 2, 2),
    "bank_account": lambda t: mask_digits_keep(t, 3, 2),
    "mobile": lambda t: mask_digits_keep(t, 4, 3),        # 0912***678
    "landline": lambda t: mask_digits_keep(t, 2, 2),      # (02)****-**89
    "email": _mask_email,
    "birthdate": _mask_date,                              # 民國**年*月**日
    "address": _mask_address,                             # 台北市大安區***
    "address_ctx": _mask_address,
    "address_bare": _mask_address,
    "plate": lambda t: mask_keep(t, 2, 0),
    "name": _mask_name,                                   # 王○○
    "name_honorific": _mask_name,
    "name_bare": _mask_name,
    "name_paren_id": _mask_name,
    "name_known": _mask_name,
    "id_bare": lambda t: mask_digits_keep(t, 2, 2),       # B1******33
    "policy_no": lambda t: mask_digits_keep(t, 2, 2),
    "phone_bare": lambda t: mask_digits_keep(t, 2, 2),
    "note_digits": lambda t: mask_digits_keep(t, 2, 2),
}


def make_replacement(match: PIIMatch, mode: str = "partial") -> str:
    """依模式產生遮罩後的替代文字。"""
    if mode == "label":
        return "[" + match.label + "]"
    if mode == "full":
        return MASK_CHAR * len(match.text)
    masker = _PARTIAL_MASKERS.get(match.type)
    if masker is None:
        return MASK_CHAR * len(match.text)
    return masker(match.text)
