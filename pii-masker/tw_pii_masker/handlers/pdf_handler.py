# -*- coding: utf-8 -*-
"""PDF 處理器（使用 PyMuPDF，全程本機執行）。

採用「真實遮蔽（redaction）」：套用後底層文字會從 PDF 內容流中移除，
無法用複製貼上或文字搜尋還原——不是只畫一個黑色方塊蓋住而已。

已知限制：
  - 掃描影像 PDF（無文字層）無法自動遮罩，會在報告中警告；需先 OCR
  - 加密（需密碼）的 PDF 請先解密再處理
  - 跨行斷開的個資可能定位不到，會在報告中警告該筆需人工處理
"""
from __future__ import annotations

import pymupdf

from ..engine import MaskedItem, MaskingEngine
from ..report import Report

# 遮罩替代文字含中文時使用的內建繁體中文字型
_CJK_FONT = "china-t"


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def mask_pdf(input_path: str, output_path: str,
             engine: MaskingEngine, report: Report,
             keep_metadata: bool = False) -> None:
    doc = pymupdf.open(input_path)
    try:
        if doc.needs_pass:
            raise ValueError("此 PDF 已加密，請先解除密碼保護再處理")

        for pno, page in enumerate(doc, 1):
            text = page.get_text("text")
            if not text.strip():
                if page.get_images(full=True):
                    report.warn("第 %d 頁沒有文字層（疑似掃描影像），無法自動遮罩，"
                                "需先 OCR 或人工處理" % pno)
                continue

            matches = engine.scan(text)
            if not matches:
                continue

            searched = set()
            location = "第 %d 頁" % pno
            for m in matches:
                repl = engine.replacement(m)
                report.add(location, MaskedItem(m.type, m.label, m.text, repl))
                if m.text in searched:
                    continue
                searched.add(m.text)

                rects = page.search_for(m.text)
                if not rects:
                    report.warn("%s：「%s」定位失敗（可能跨行斷開），請人工確認遮罩"
                                % (location, repl))
                    continue
                for rect in rects:
                    fontname = None if _is_ascii(repl) else _CJK_FONT
                    try:
                        page.add_redact_annot(
                            rect, text=repl, fontname=fontname,
                            fill=(1, 1, 1), text_color=(0, 0, 0), cross_out=False)
                    except Exception:
                        # 字型或版本問題時退回黑色色塊（仍會移除底層文字）
                        page.add_redact_annot(rect, fill=(0, 0, 0), cross_out=False)

            # 只移除文字，不動到影像，避免破壞版面上的圖片
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

        if not keep_metadata:
            meta = doc.metadata or {}
            if meta.get("author") or meta.get("creator"):
                report.warn("已清除 PDF 中的作者／建立者資訊")
            doc.set_metadata({})

        doc.save(output_path, garbage=3, deflate=True)
    finally:
        doc.close()
