# -*- coding: utf-8 -*-
"""端對端測試：實際產生 docx / xlsx / pdf 檔並執行 CLI 遮罩。"""
import sys
import zipfile
from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).parent.parent / "samples"
sys.path.insert(0, str(SAMPLES_DIR))

import make_samples  # noqa: E402
from tw_pii_masker.cli import run  # noqa: E402

FAKE = make_samples.FAKE


@pytest.fixture()
def sample_dir(tmp_path):
    make_samples.make_docx(tmp_path)
    make_samples.make_xlsx(tmp_path)
    make_samples.make_pdf(tmp_path)
    return tmp_path


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_docx_masking(sample_dir):
    src = sample_dir / "示範_理賠申請書.docx"
    rc = run([str(src)])
    assert rc == 0
    out = sample_dir / "示範_理賠申請書_masked.docx"
    assert out.exists()
    xml = _docx_text(out)
    for key in ("id", "mobile", "email", "card"):
        assert FAKE[key] not in xml, key
    assert "王小明" not in xml
    assert "和平東路" not in xml
    # 部分遮罩應保留可比對的頭尾
    assert "A12" in xml
    assert "王○○" in xml
    # 原始檔不得被修改
    assert FAKE["id"] in _docx_text(src)


def test_xlsx_masking(sample_dir):
    from openpyxl import load_workbook
    src = sample_dir / "示範_客戶名單.xlsx"
    rc = run([str(src)])
    assert rc == 0
    out = sample_dir / "示範_客戶名單_masked.xlsx"
    wb = load_workbook(out)
    ws = wb["客戶名單"]
    values = " | ".join(str(c.value) for row in ws.iter_rows() for c in row)
    assert FAKE["id"] not in values
    assert "0912-345-678" not in values
    assert "王小明" not in values
    assert "04595257" not in values          # 統編（有「統一編號」前後文）
    assert "李大華" not in values            # 要保人欄位觸發姓名偵測
    assert "A12" in values                   # 部分遮罩保留頭
    # 表頭「姓名」「身分證字號」等欄位名稱不應被遮罩
    assert "姓名" in values


def test_pdf_masking(sample_dir):
    import pymupdf
    src = sample_dir / "示範_契約通知書.pdf"
    rc = run([str(src)])
    assert rc == 0
    out = sample_dir / "示範_契約通知書_masked.pdf"
    doc = pymupdf.open(out)
    text = "".join(page.get_text() for page in doc)
    doc.close()
    for key in ("id", "mobile", "email"):
        assert FAKE[key] not in text, key
    assert "王小明" not in text
    # redaction 為真實移除：原文以文字搜尋也找不到
    assert "A123456789" not in text


def test_dry_run_outputs_nothing(sample_dir):
    src = sample_dir / "示範_理賠申請書.docx"
    rc = run([str(src), "--dry-run"])
    assert rc == 0
    assert not (sample_dir / "示範_理賠申請書_masked.docx").exists()


def test_report_json(sample_dir, tmp_path):
    import json
    src = sample_dir / "示範_理賠申請書.docx"
    report_file = tmp_path / "report.json"
    rc = run([str(src), "--report", str(report_file)])
    assert rc == 0
    data = json.loads(report_file.read_text(encoding="utf-8"))
    findings = data["files"][0]["findings"]
    assert findings
    # 預設不得寫入原始個資
    assert all("original" not in f for f in findings)
    assert data["files"][0]["counts"].get("身分證字號", 0) >= 1


def test_label_mode(sample_dir):
    src = sample_dir / "示範_理賠申請書.docx"
    rc = run([str(src), "-m", "label", "--suffix", "_label"])
    assert rc == 0
    xml = _docx_text(sample_dir / "示範_理賠申請書_label.docx")
    assert "[身分證字號]" in xml


def test_txt_masking(tmp_path):
    src = tmp_path / "note.txt"
    src.write_text("客戶王先生 身分證 A123456789 電話0912345678", encoding="utf-8")
    rc = run([str(src)])
    assert rc == 0
    out = (tmp_path / "note_masked.txt").read_text(encoding="utf-8")
    assert "A123456789" not in out
    assert "0912345678" not in out


def test_xlsx_numeric_phone_and_bare_address(tmp_path):
    from openpyxl import Workbook, load_workbook
    src = tmp_path / "名單.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["姓名", "行動電話", "地址"])
    ws.append(["王小明", 912345678, "板橋區文化路一段23號5樓"])  # 電話存成數值、地址未含縣市
    wb.save(src)
    rc = run([str(src)])
    assert rc == 0
    out = load_workbook(tmp_path / "名單_masked.xlsx").active
    values = " | ".join(str(c.value) for row in out.iter_rows() for c in row)
    assert "912345678" not in values
    assert "0912****78" in values      # 補回開頭的 0 再遮罩
    assert "文化路" not in values
    assert "板橋區" in values
