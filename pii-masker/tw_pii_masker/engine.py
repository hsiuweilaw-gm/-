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

內部人員豁免
------------
姓名是否遮罩，先看「這個人」再看「這一處」：

  1. 只要該姓名在本文件任一處是客戶（保戶、要保人、被保險人…），
     則每一處都遮——包含「帳號 X」處。因為同一格內兩處常共用同一個
     身分證，保留其中一處的姓名等於把另一處的遮罩解開。
  2. 從未以客戶身分出現過的純內部人員（業務員、帳號、經手人…），
     各處都不遮，含無標籤處。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from . import detectors, masking
from .detectors import PIIMatch

# 會被登記、進而全文件一致遮罩的類型（皆為姓名類）
_LEARNABLE_TYPES = ("name", "name_customer", "name_paren_id",
                    "name_honorific", "name_bare")


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
                # 此處身分為內部人員（業務員／帳號／經手人…）
                self._agent_names.add(m.text)
            elif (self.mask_agent_names
                    or detectors.is_customer_context(text, m.start, context_hint)):
                # 此處身分明確為客戶：登記起來，供無標籤處的一致性遮罩。
                # 無標籤處本身不算證據，不列入任一方
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
        """本文件中曾以內部人員（業務員／帳號／經手人…）身分出現的姓名。
        僅供報告參考——實際是否遮罩以每一處的標籤逐處判斷。"""
        return set(self._agent_names)

    @property
    def known_names(self) -> Set[str]:
        return set(self._known_names)

    def _known_pattern(self) -> Optional["re.Pattern[str]"]:
        if not self._known_names:
            return None
        if self._known_re is None:
            # 長字串優先，避免「林宥」先於「林宥慈」命中
            alts = sorted(self._known_names, key=len, reverse=True)
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
            matches = [m for m in matches
                       if not m.type.startswith("name")
                       or self._should_mask_name(m.text, text, m.start, context_hint)]
        pattern = self._known_pattern()
        if pattern is None:
            return matches
        extra: List[PIIMatch] = []
        for m in pattern.finditer(text):
            start, end = m.span()
            if any(start < k.end and k.start < end for k in matches):
                continue
            if not self.mask_agent_names and not self._should_mask_name(
                    m.group(0), text, start, context_hint):
                continue   # 該處身分為內部人員，不補遮
            # 標籤保持簡潔：label 模式會把它直接寫進文件
            extra.append(PIIMatch("name_known", "姓名", start, end, m.group(0), 48))
        if not extra:
            return matches
        merged = matches + extra
        merged.sort(key=lambda x: x.start)
        return merged

    def _should_mask_name(self, name: str, text: str, start: int,
                          context_hint: str) -> bool:
        """判斷某處姓名是否該遮罩。

        1. 該姓名在本文件任一處是客戶 → 每一處都遮，含「帳號 X」處。
           客戶身分具全文件優先性，否則遮罩會被自己還原：

               要保人 饒○○(F22****519) … 帳號 饒書寧(F22****519)

           兩處身分證尾碼相同，保留右邊那個姓名等於把左邊的遮罩解開。
        2. 否則，該處標籤是內部人員（帳號／業務員／經手人…）→ 不遮
        3. 純內部人員的姓名（從未以客戶身分出現）→ 各處都不遮，
           含無標籤處（如 J 欄「經手人 葉珍玲」、K 欄無標籤的葉珍玲）
        4. 其餘（無標籤且身分不明）→ 從嚴遮罩
        """
        if name in self._known_names:
            return True
        if detectors.is_agent_context(text, start, context_hint):
            return False
        return name not in self._agent_names

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
