# -*- coding: utf-8 -*-
"""遮罩引擎：整合偵測與遮罩，供各檔案格式處理器共用。

一致性遮罩（consistency masking）
--------------------------------
姓名在自由文字中往往只有部分出現處帶有標籤，例如：

    要保人 林宥慈(F220755862):手機 0920094377 與 帳號 林宥慈(F220755862) 相同

若只遮「要保人」後那一次，同段文字後面仍留著明文姓名，遮罩形同虛設。
因此本引擎採兩階段：

  1. learn()：先掃過整份文件，把確認為姓名的字串登記起來
  2. mask_text()：除了本文偵測，另把所有已登記姓名的出現處一併遮罩

各檔案處理器負責在遮罩前先跑一遍 learn()。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from . import detectors, masking
from .detectors import PIIMatch

# 會被登記、進而全文件一致遮罩的類型（皆為姓名類）
_LEARNABLE_TYPES = ("name", "name_paren_id", "name_honorific", "name_bare")


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
                 all_dates: bool = False,
                 include: Optional[Sequence[str]] = None,
                 mask_agent_names: bool = False):
        """types 指定時只啟用該些類型；未指定則啟用預設類型集
        （全部類型扣除 DEFAULT_OFF_TYPES）。include 可額外開啟預設關閉的類型。"""
        if mode not in masking.MODES:
            raise ValueError("未知的遮罩模式: %s（可用：%s）" % (mode, "/".join(masking.MODES)))
        if types:
            enabled = set(types)
        else:
            enabled = set(detectors.ALL_TYPES) - set(detectors.DEFAULT_OFF_TYPES)
        if include:
            enabled |= set(include)
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
        # False（預設）：業務員／帳號情境的姓名不遮罩，視為內部人員而非客戶個資
        self.mask_agent_names = mask_agent_names
        self._known_names: Set[str] = set()
        self._known_re: Optional["re.Pattern[str]"] = None
        # 於文件中任一處被判定為業務員／帳號的姓名，全文件均不遮罩
        self._agent_names: Set[str] = set()

    # ------------------------------------------------------------------
    # 第一階段：學習本文件中的姓名
    # ------------------------------------------------------------------
    def learn(self, text: str, context_hint: str = "") -> None:
        """掃描一段文字，把確認為姓名者登記起來供全文件一致遮罩。"""
        if not text:
            return
        for m in self._detect(text, context_hint):
            if m.type not in _LEARNABLE_TYPES or len(m.text) < 2:
                continue
            if not self.mask_agent_names and detectors.is_agent_context(
                    text, m.start, context_hint):
                # 業務員／帳號姓名：登記為豁免，全文件都不遮
                self._agent_names.add(m.text)
                self._known_names.discard(m.text)
            elif m.text not in self._agent_names:
                self._known_names.add(m.text)
            self._known_re = None

    def learn_all(self, texts: Iterable[Tuple[str, str]]) -> None:
        """批次學習，texts 為 (文字, 前後文提示) 的序列。"""
        for text, hint in texts:
            self.learn(text, hint)

    def reset_learned(self) -> None:
        """清空已登記姓名。一致性遮罩以「單一文件」為範圍，
        各檔案處理器於開始處理前呼叫，避免跨檔案累積。"""
        self._known_names.clear()
        self._agent_names.clear()
        self._known_re = None

    @property
    def agent_names(self) -> Set[str]:
        """本文件中判定為業務員／帳號、因而不遮罩的姓名。"""
        return set(self._agent_names)

    @property
    def known_names(self) -> Set[str]:
        return set(self._known_names)

    def _known_pattern(self) -> Optional["re.Pattern[str]"]:
        if not self._known_names or self._known_names <= self._agent_names:
            return None
        if self._known_re is None:
            # 長字串優先，避免「林宥」先於「林宥慈」命中
            names = self._known_names - self._agent_names
            if not names:
                return None
            alts = sorted(names, key=len, reverse=True)
            self._known_re = re.compile("|".join(re.escape(n) for n in alts))
        return self._known_re

    # ------------------------------------------------------------------
    # 偵測與遮罩
    # ------------------------------------------------------------------
    def _detect(self, text: str, context_hint: str = "") -> List[PIIMatch]:
        return detectors.scan(text, enabled=self.enabled,
                              all_dates=self.all_dates, context_hint=context_hint)

    def scan(self, text: str, context_hint: str = "") -> List[PIIMatch]:
        """偵測個資，並補上本文件已登記姓名的其餘出現處。

        業務員／帳號情境的姓名（以及已登記為業務員的姓名）不納入遮罩。
        """
        matches = self._detect(text, context_hint)
        if not self.mask_agent_names:
            matches = [
                m for m in matches
                if not m.type.startswith("name")
                or (m.text not in self._agent_names
                    and not detectors.is_agent_context(text, m.start, context_hint))
            ]
        pattern = self._known_pattern()
        if pattern is None:
            return matches
        extra: List[PIIMatch] = []
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < k.end and k.start < end for k in matches):
                continue
            # 標籤保持簡潔：label 模式會把它直接寫進文件
            extra.append(PIIMatch("name_known", "姓名", start, end, m.group(0), 48))
        if not extra:
            return matches
        merged = matches + extra
        merged.sort(key=lambda x: x.start)
        return merged

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
