# -*- coding: utf-8 -*-
"""遮罩引擎：整合偵測與遮罩，供各檔案格式處理器共用。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import detectors, masking
from .detectors import PIIMatch


@dataclass
class MaskedItem:
    """一筆已遮罩的個資紀錄（供報告使用）。"""
    type: str
    label: str
    original: str
    replacement: str


class MaskingEngine:
    def __init__(self,
                 types: Optional[Sequence[str]] = None,
                 exclude: Optional[Sequence[str]] = None,
                 mode: str = "partial",
                 all_dates: bool = False):
        if mode not in masking.MODES:
            raise ValueError("未知的遮罩模式: %s（可用：%s）" % (mode, "/".join(masking.MODES)))
        enabled = set(types) if types else set(detectors.ALL_TYPES)
        if exclude:
            enabled -= set(exclude)
        unknown = enabled - set(detectors.ALL_TYPES)
        if unknown:
            raise ValueError("未知的個資類型: %s（可用：%s）"
                             % (", ".join(sorted(unknown)),
                                ", ".join(detectors.ALL_TYPES)))
        self.enabled = enabled
        self.mode = mode
        self.all_dates = all_dates

    def scan(self, text: str, context_hint: str = "") -> List[PIIMatch]:
        return detectors.scan(text, enabled=self.enabled,
                              all_dates=self.all_dates, context_hint=context_hint)

    def replacement(self, match: PIIMatch) -> str:
        return masking.make_replacement(match, self.mode)

    def mask_text(self, text: str, context_hint: str = "") -> Tuple[str, List[MaskedItem]]:
        """回傳（遮罩後文字, 遮罩紀錄清單）。

        context_hint 為外部前後文（表格欄位標題、左側儲存格文字等），
        只用於判斷、不會出現在輸出。"""
        matches = self.scan(text, context_hint=context_hint)
        if not matches:
            return text, []
        items: List[MaskedItem] = []
        pieces: List[str] = []
        cursor = 0
        for m in matches:
            repl = self.replacement(m)
            pieces.append(text[cursor:m.start])
            pieces.append(repl)
            cursor = m.end
            items.append(MaskedItem(m.type, m.label, m.text, repl))
        pieces.append(text[cursor:])
        return "".join(pieces), items
