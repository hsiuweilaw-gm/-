# -*- coding: utf-8 -*-
"""遮罩結果報告：主控台摘要 + 可選 JSON 報告檔。

預設報告「不」記錄原始個資內容（只記錄遮罩後樣貌），
需加 --show-original 才會寫入原文，避免報告本身變成個資外洩源。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .engine import MaskedItem


@dataclass
class Finding:
    location: str
    type: str
    label: str
    replacement: str
    original: Optional[str] = None


@dataclass
class FileReport:
    input_path: str
    output_path: Optional[str] = None
    findings: List[Finding] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def counts(self) -> Dict[str, int]:
        return dict(Counter(f.label for f in self.findings))


class Report:
    def __init__(self, show_original: bool = False, mode: str = "partial"):
        self.show_original = show_original
        self.mode = mode
        self.files: List[FileReport] = []
        self._current: Optional[FileReport] = None

    def start_file(self, input_path: str) -> FileReport:
        self._current = FileReport(input_path=str(input_path))
        self.files.append(self._current)
        return self._current

    def add(self, location: str, item: MaskedItem) -> None:
        assert self._current is not None
        self._current.findings.append(Finding(
            location=location,
            type=item.type,
            label=item.label,
            replacement=item.replacement,
            original=item.original if self.show_original else None,
        ))

    def add_all(self, location: str, items: List[MaskedItem]) -> None:
        for it in items:
            self.add(location, it)

    def warn(self, message: str) -> None:
        assert self._current is not None
        self._current.warnings.append(message)

    def set_output(self, output_path: str) -> None:
        assert self._current is not None
        self._current.output_path = str(output_path)

    def set_error(self, message: str) -> None:
        assert self._current is not None
        self._current.error = message

    # ------------------------------------------------------------------
    def print_summary(self) -> None:
        # 輸出僅使用 ASCII 符號裝飾，避免 Windows 主控台（cp950）無法顯示
        total = 0
        for fr in self.files:
            print("-" * 60)
            print("檔案：%s" % fr.input_path)
            if fr.error:
                print("  [失敗] %s" % fr.error)
                continue
            counts = fr.counts()
            n = len(fr.findings)
            total += n
            if not counts:
                print("  未偵測到個資。")
            else:
                for label, c in sorted(counts.items(), key=lambda kv: -kv[1]):
                    print("  - %s：%d 筆" % (label, c))
                print("  共遮罩 %d 筆" % n)
            for w in fr.warnings:
                print("  [注意] %s" % w)
            if fr.output_path:
                print("  輸出：%s" % fr.output_path)
        print("-" * 60)
        print("完成，共處理 %d 個檔案、遮罩 %d 筆個資。" % (len(self.files), total))

    def to_json(self) -> str:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": self.mode,
            "contains_original": self.show_original,
            "files": [
                {
                    "input": fr.input_path,
                    "output": fr.output_path,
                    "error": fr.error,
                    "counts": fr.counts(),
                    "warnings": fr.warnings,
                    "findings": [
                        {k: v for k, v in {
                            "location": f.location,
                            "type": f.type,
                            "label": f.label,
                            "masked": f.replacement,
                            "original": f.original,
                        }.items() if v is not None}
                        for f in fr.findings
                    ],
                }
                for fr in self.files
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())
