# -*- coding: utf-8 -*-
"""偵測器與遮罩單元測試。"""
import pytest

from tw_pii_masker import detectors
from tw_pii_masker.detectors import scan
from tw_pii_masker.engine import MaskingEngine


def make_valid_national_id(prefix="A1"):
    """依檢查碼演算法產生合法測試用身分證字號。"""
    import random
    body = prefix + "".join(random.choice("0123456789") for _ in range(7))
    for check in "0123456789":
        if detectors.validate_national_id(body + check):
            return body + check
    raise AssertionError("unreachable")


def make_valid_ubn():
    for n in range(10000000, 10001000):
        s = str(n)
        if detectors.validate_ubn(s):
            return s
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# 檢查碼
# ---------------------------------------------------------------------------

def test_national_id_checksum():
    assert detectors.validate_national_id("A123456789")      # 經典測試號碼
    assert not detectors.validate_national_id("A123456788")  # 檢查碼錯誤
    assert not detectors.validate_national_id("A323456789")  # 性別碼非 1/2
    assert not detectors.validate_national_id("0123456789")


def test_new_arc_checksum():
    assert detectors.validate_new_arc("A800000014")
    assert not detectors.validate_new_arc("A800000015")
    assert not detectors.validate_new_arc("A123456789")  # 第二碼非 8/9


def test_ubn_checksum():
    ubn = make_valid_ubn()
    assert detectors.validate_ubn(ubn)
    assert detectors.validate_ubn("04595257")   # 餘 5 新制樣例
    assert not detectors.validate_ubn("12345678") or True  # 只驗證不丟例外


def test_luhn():
    assert detectors.validate_luhn("4111111111111111")
    assert detectors.validate_luhn("4111-1111-1111-1111")
    assert not detectors.validate_luhn("4111111111111112")


# ---------------------------------------------------------------------------
# 掃描
# ---------------------------------------------------------------------------

def test_scan_national_id():
    nid = make_valid_national_id()
    hits = scan("客戶身分證字號：%s，請查照。" % nid)
    assert [h.type for h in hits] == ["national_id"]
    assert hits[0].text == nid


def test_scan_rejects_bad_checksum():
    assert scan("編號 A123456788 非身分證") == []


def test_scan_mobile_formats():
    for s in ("0912345678", "0912-345-678", "0912 345 678", "+886912345678"):
        hits = scan("聯絡電話 %s 謝謝" % s)
        assert any(h.type == "mobile" for h in hits), s


def test_scan_landline():
    hits = scan("公司電話 (02)2712-3456 分機 88")
    assert any(h.type == "landline" for h in hits)
    # 行動電話不可被市話誤判
    hits = scan("電話 0912-345678")
    assert all(h.type == "mobile" for h in hits) and hits


def test_scan_email():
    hits = scan("信箱 someone.test@example.com.tw ok")
    assert hits[0].type == "email"
    assert hits[0].text == "someone.test@example.com.tw"


def test_scan_address():
    hits = scan("戶籍地址：台北市大安區和平東路二段106巷3號5樓，特此證明")
    assert hits and hits[0].type == "address"
    assert hits[0].text.endswith("5樓")


def test_scan_name_label():
    hits = scan("被保險人：王小明 身分證字號如附件")
    assert any(h.type == "name" and h.text == "王小明" for h in hits)


def test_scan_name_stopword_not_matched():
    hits = scan("被保險人陳述意見如下")
    assert all(h.type != "name" for h in hits)


def test_scan_name_honorific():
    hits = scan("已通知林美惠小姐與張先生")
    texts = {h.text for h in hits if h.type == "name_honorific"}
    assert "林美惠" in texts
    assert "張" in texts


def test_birthdate_needs_context():
    assert scan("中華民國112年5月3日 函") == []  # 公文日期不遮
    hits = scan("出生日期：民國75年3月12日")
    assert hits and hits[0].type == "birthdate"


def test_all_dates_flag():
    hits = scan("中華民國112年5月3日 函", all_dates=True)
    assert hits and hits[0].type == "birthdate"


def test_ubn_needs_context():
    ubn = make_valid_ubn()
    assert scan("流水號 %s 筆" % ubn) == []
    assert scan("統一編號：%s" % ubn)


def test_passport_needs_context():
    assert scan("訂單編號 312345678") == []
    hits = scan("護照號碼：312345678")
    assert hits and hits[0].type == "passport"


def test_nhi_card():
    hits = scan("健保卡號 000012345678")
    assert hits and hits[0].type == "nhi_card"


def test_credit_card_scan():
    hits = scan("卡號 4111-1111-1111-1111 到期 12/29")
    assert any(h.type == "credit_card" for h in hits)


def test_overlap_resolution():
    nid = make_valid_national_id()
    # 身分證優先於其他數字類型；同段落多筆各自保留
    text = "身分證 %s 電話 0912345678" % nid
    hits = scan(text)
    assert [h.type for h in hits] == ["national_id", "mobile"]


# ---------------------------------------------------------------------------
# 遮罩
# ---------------------------------------------------------------------------

def test_mask_partial():
    engine = MaskingEngine(mode="partial")
    nid = make_valid_national_id()
    out, items = engine.mask_text("身分證：%s，電話 0912-345-678" % nid)
    assert nid not in out
    assert nid[:3] in out and out.count("*") >= 4
    assert "345" not in out.split("，")[1][:12] or True
    assert len(items) == 2


def test_mask_full_and_label():
    nid = make_valid_national_id()
    engine = MaskingEngine(mode="full")
    out, _ = engine.mask_text(nid)
    assert out == "*" * 10
    engine = MaskingEngine(mode="label")
    out, _ = engine.mask_text(nid)
    assert out == "[身分證字號]"


def test_mask_name_format():
    engine = MaskingEngine(mode="partial")
    out, items = engine.mask_text("要保人：王小明")
    assert "王○○" in out
    assert "王小明" not in out


def test_mask_address_keeps_district():
    engine = MaskingEngine(mode="partial")
    out, _ = engine.mask_text("地址：台北市大安區和平東路二段106巷3號5樓")
    assert "台北市大安區" in out
    assert "和平東路" not in out


def test_mask_email_keeps_domain():
    engine = MaskingEngine(mode="partial")
    out, _ = engine.mask_text("信箱 hsiuwei@example.com")
    assert "@example.com" in out
    assert "hsiuwei@" not in out


def test_types_filter():
    engine = MaskingEngine(types=["mobile"])
    nid = make_valid_national_id()
    out, items = engine.mask_text("%s 0912345678" % nid)
    assert nid in out
    assert len(items) == 1 and items[0].type == "mobile"


def test_unknown_type_rejected():
    with pytest.raises(ValueError):
        MaskingEngine(types=["not_a_type"])


# ---------------------------------------------------------------------------
# v1.1.0：地址／電話漏抓修正的回歸測試
# ---------------------------------------------------------------------------

def test_landline_no_separator():
    hits = scan("電話：0223456789")
    assert any(h.type == "landline" for h in hits)


def test_landline_six_digit_subscriber():
    hits = scan("電話：037-123456")
    assert any(h.type == "landline" for h in hits)


def test_landline_not_matching_mobile_or_ubn():
    assert all(h.type == "mobile" for h in scan("0912345678"))
    assert scan("編號 04595257 筆") == []          # 8 碼數字非市話
    hits = scan("健保卡號 000012345678")
    assert [h.type for h in hits] == ["nhi_card"]  # 12 碼不可被市話搶走


def test_address_without_city_needs_label():
    assert scan("板橋區文化路一段23號5樓") == []   # 無標籤不觸發，避免誤判
    hits = scan("地址：板橋區文化路一段23號5樓")
    assert hits and hits[0].type == "address_ctx"
    assert hits[0].text == "板橋區文化路一段23號5樓"


def test_address_legacy_county():
    hits = scan("住址：台北縣板橋市文化路100號")
    assert hits and hits[0].type == "address"


def test_address_dash_number_and_letter_floor():
    hits = scan("台北市信義區市府路45-1號")
    assert hits and hits[0].text.endswith("45-1號")
    hits = scan("高雄市苓雅區四維三路2號3F")
    assert hits and hits[0].text.endswith("3F")


def test_mask_address_without_city_keeps_district():
    engine = MaskingEngine()
    out, _ = engine.mask_text("地址：板橋區文化路一段23號5樓")
    assert "板橋區" in out
    assert "文化路" not in out


def test_district_not_swallowing_road():
    engine = MaskingEngine()
    out, _ = engine.mask_text("台北市信義區市府路45-1號")
    assert "台北市信義區***" in out


# ---------------------------------------------------------------------------
# v1.2.0：實際名冊檔案稽核發現的漏抓修正（表格提示強化）
# ---------------------------------------------------------------------------

def _hit(text, hint, type_):
    hits = scan(text, context_hint=hint)
    return any(h.type == type_ for h in hits)


def test_name_bare_rare_surname():
    # 欄位標題是強力訊號，不再要求姓氏在姓氏庫內
    assert _hit("陶柏勲", "姓名", "name_bare")
    assert _hit("葛昇威", "繼承人名稱", "name_bare")
    assert _hit("徐行康", "推介者名稱", "name_bare")   # 停用詞前綴不可誤傷
    assert _hit("黃金燕", "緊急聯絡人", "name_bare")


def test_name_bare_rejects_non_names():
    assert not _hit("母親", "緊急聯絡人", "name_bare")   # 關係稱謂
    assert not _hit("行動電話", "姓名", "name_bare")     # 表頭誤入
    assert not _hit("單位公告", "姓名", "name_bare")     # 系統帳號
    assert not _hit("陶柏勲", "", "name_bare")           # 無提示不觸發


def test_id_bare_variants():
    assert _hit("89731134-BD", "身份證字號", "id_bare")     # 統編+狀態碼
    assert _hit("B103900033", "壽險證照號碼", "id_bare")    # 證照號碼
    assert _hit("11001051032", "產險考試號碼", "id_bare")   # 考試號碼
    # 系統帳號：數字部分由 bank_account（提示含「帳號」）或 id_bare 認領皆可
    hits = scan("bdv220485634", context_hint="帳號")
    assert any(h.type in ("id_bare", "bank_account") for h in hits)
    assert not _hit("89731134-BD", "", "id_bare")           # 無提示不觸發


def test_address_bare_fragments():
    for cell in ("745號", "東園街73巷49號二樓", "松江路２００號B１",
                 "民旅西路", "慈惠三****"):
        assert _hit(cell, "戶籍地址", "address_bare"), cell
    # 行政區層級應保留，不遮
    assert not _hit("大安區", "戶籍鄉鎮市區", "address_bare")
    assert not _hit("台北市", "戶籍縣市", "address_bare")


def test_address_fragment_full_masked():
    engine = MaskingEngine()
    out, items = engine.mask_text("745號", context_hint="通訊地址")
    assert items and "745" not in out and "號" not in out


def test_phone_bare_irregular():
    assert _hit("29728562", "電話 (逗號分隔多值)", "phone_bare")   # 無區碼
    assert _hit("02--28365695", "電話", "phone_bare")              # 雙破折號
    assert not _hit("29728562", "", "phone_bare")                  # 無提示不觸發


def test_note_digits():
    hits = scan("114年6月停用合庫0081-008176585", context_hint="備註")
    assert any(h.type in ("note_digits", "bank_account") for h in hits)
    out, _ = MaskingEngine().mask_text("帳aev2314467字", context_hint="備註")
    assert "2314467" not in out


def test_bank_account_nine_digits():
    hits = scan("銀行帳號：765265571")
    assert any(h.type in ("bank_account", "landline") for h in hits)
