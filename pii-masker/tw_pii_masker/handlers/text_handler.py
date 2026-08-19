# -*- coding: utf-8 -*-
"""純文字 / CSV 處理器。

以 UTF-8 讀取，失敗時退回 CP950（Big5，台灣舊系統匯出常見編碼），
輸出時沿用讀入的編碼。
"""
from __future__ import annotations

from ..engine import MaskingEngine
from ..report import Report

_ENCODINGS = ("utf-8-sig", "cp950")


def mask_text_file(input_path: str, output_path: str,
                   engine: MaskingEngine, report: Report,
                   keep_metadata: bool = False) -> None:
    content = None
    used = None
    for enc in _ENCODINGS:
        try:
            with open(input_path, "r", encoding=enc) as fh:
                content = fh.read()
            used = enc
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if content is None:
        raise ValueError("無法辨識檔案編碼（已嘗試 UTF-8 與 Big5/CP950）")

    lines = content.splitlines(keepends=True)
    out_lines = []
    for i, line in enumerate(lines, 1):
        new_line, items = engine.mask_text(line)
        if items:
            report.add_all("第 %d 行" % i, items)
        out_lines.append(new_line)

    with open(output_path, "w", encoding=used) as fh:
        fh.write("".join(out_lines))
