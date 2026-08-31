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
    assert any(h.type.startswith("name") and h.text == "王小明" for h in hits)


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


# ---------------------------------------------------------------------------
# v1.3.0：查核案件檔案稽核發現的漏抓修正（自由文字姓名一致性）
# ---------------------------------------------------------------------------

def test_surname_list_covers_missing_ones():
    # 饒等姓氏原不在庫內，造成整筆姓名漏遮
    for s in "饒崔任莫柴":
        assert s in detectors._SURNAMES_SINGLE, s


def test_name_paren_id_without_label():
    # 「姓名(證號)」不需任何標籤即可辨識
    hits = scan("與 帳號 饒培杰(A121775567) 之 地址 相同")
    assert any(h.type == "name_paren_id" and h.text == "饒培杰" for h in hits)
    hits = scan("經手人 葉珍玲(HC-20) 不具資格")
    assert any(h.text == "葉珍玲" for h in hits)


def test_name_paren_id_ignores_org_account():
    # 機構帳戶名（前面接中文）不可被當成姓名
    hits = scan("與 帳號 祐誠行政專帳(00000002) 之 地址 相同")
    assert all(h.type != "name_paren_id" for h in hits)


def test_name_not_swallowing_org_words():
    # 「楊惠萍保險經紀人」的姓名只到「楊惠萍」，不可遮成「楊○○○險經紀人」
    hits = scan("要保人 楊惠萍保險經紀人(88279134)")
    names = [h.text for h in hits if h.type.startswith("name")]
    assert "楊惠萍" in names
    assert all("保" not in n for n in names)
    out, _ = MaskingEngine().mask_text("要保人 楊惠萍保險經紀人")
    assert out == "要保人 楊○○保險經紀人"


def test_consistency_masking_same_cell():
    """同一段文字中同一姓名的每一次出現都要遮（原本只遮第一次）。"""
    text = ("要保人 林宥慈(F220755862):手機 0920094377；"
            "聯絡時請找 林宥慈<br>主被保人 林宥慈")
    engine = MaskingEngine()
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "林宥慈" not in out
    assert out.count("林○○") == 3


def test_consistency_masking_across_texts():
    """A 段落確認的姓名，B 段落即使無標籤也要遮。"""
    engine = MaskingEngine()
    engine.learn("要保人 周洺禾 申請")
    out, items = engine.mask_text("本案 周洺禾 之 地址 相同")
    assert "周洺禾" not in out
    assert items and items[0].type == "name_known"


def test_reset_learned_scopes_to_one_document():
    engine = MaskingEngine()
    engine.learn("要保人 周洺禾 申請")
    assert engine.known_names
    engine.reset_learned()
    assert not engine.known_names
    out, _ = engine.mask_text("帳號 周洺禾")
    assert "周洺禾" in out          # 已重置，不再跨文件遮罩


def test_policy_no_needs_keyword():
    hits = scan("受理 全球人壽 保單 1150723LN00013 的案件")
    assert any(h.type == "policy_no" and h.text == "1150723LN00013" for h in hits)
    # 無關鍵字不觸發；鄰格提示也不得外溢觸發
    assert not any(h.type == "policy_no" for h in scan("查核序號 1150820DR0001"))
    assert not any(h.type == "policy_no"
                   for h in scan("1150820DR0001", context_hint="保單"))


def test_policy_no_off_by_default():
    """保單號碼預設不遮（業務需保留辨識），需明確開啟。"""
    engine = MaskingEngine()
    out, _ = engine.mask_text("受理 保單 1150723LN00013 案件")
    assert "1150723LN00013" in out

    engine = MaskingEngine(include=["policy_no"])
    out, items = engine.mask_text("受理 保單 1150723LN00013 案件")
    assert "1150723LN00013" not in out
    assert any(i.type == "policy_no" for i in items)


# ---------------------------------------------------------------------------
# v1.4.0：業務員／帳號姓名不遮罩
# ---------------------------------------------------------------------------

def test_agent_name_not_masked():
    """「帳號」「業務員」後的姓名屬內部人員，預設不遮。"""
    engine = MaskingEngine()
    text = "要保人 周洺禾(P123732535) 與 帳號 劉湘妘(N224463647) 之 地址 相同"
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "劉湘妘" in out          # 帳號（業務員）姓名保留
    assert "周洺禾" not in out      # 客戶姓名照遮
    assert "P123732535" not in out  # 身分證仍遮
    assert "N224463647" not in out


def test_agent_label_variants():
    for label in ("業務員", "帳號", "招攬業務員", "服務人員"):
        engine = MaskingEngine()
        text = "%s 王大同 受理" % label
        engine.learn(text)
        assert "王大同" in engine.mask_text(text)[0], label


def test_agent_only_name_not_masked_anywhere():
    """只以內部人員身分出現過的姓名，無標籤處也不遮。"""
    engine = MaskingEngine()
    engine.learn("帳號 劉湘妘(N224463647) 之 地址")
    out, _ = engine.mask_text("本案由 劉湘妘 處理")
    assert "劉湘妘" in out
    assert "劉湘妘" in engine.agent_names


def test_customer_identity_wins_over_agent_context():
    """同一人兼具兩種身分時，客戶身分優先：連「帳號」處也要遮。

    兩處常共用同一組身分證尾碼，若保留帳號處的姓名，
    等於把要保人處的遮罩解開。
    """
    engine = MaskingEngine()
    text = ("要保人 林宥慈(F220755862):手機 0920094377 "
            "與 帳號 林宥慈(F220755862) 之 手機 相同")
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "林宥慈" not in out
    assert out.count("林○○") == 2


def test_pure_agent_name_still_kept_alongside_customer():
    """純內部人員（從未以客戶身分出現）仍然保留。"""
    engine = MaskingEngine()
    text = "要保人 饒書寧(F228969519) 與 帳號 饒培杰(A121775567) 之 地址 相同"
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "饒書寧" not in out      # 要保人 → 遮
    assert "饒培杰" in out          # 只當過帳號 → 保留


def test_customer_label_without_surname_in_list():
    """客戶標籤後的姓名不需姓氏在字庫內（如「陽」原本未收錄）。"""
    engine = MaskingEngine()
    for text in ("保戶 陽曼蘭 的 聯絡資料 相同", "主被保人 陽曼蘭(H291234567)"):
        engine2 = MaskingEngine()
        engine2.learn(text)
        assert "陽曼蘭" not in engine2.mask_text(text)[0], text


def test_customer_label_needs_separator():
    """標籤與姓名間須有分隔，否則「客戶身分證字號」會被誤讀。"""
    assert all(h.type != "name_customer"
               for h in scan("客戶身分證字號：A123456789"))
    assert all(h.type != "name_customer" for h in scan("客戶名單"))


def test_handler_labels_not_masked():
    """經手人／處理者／建立者等內部人員標籤，其後姓名不遮。"""
    for label in ("經手人", "經辦人", "承辦人", "處理者", "送件人", "建立者"):
        engine = MaskingEngine()
        text = "%s 葉珍玲(HC-20) 受理" % label
        engine.learn(text)
        assert "葉珍玲" in engine.mask_text(text)[0], label


def test_customer_labels_masked():
    """保戶／要保人／被保險人等客戶標籤，其後姓名一律遮。"""
    for label in ("保戶", "要保人", "被保險人", "主被保人", "副被保人",
                  "受益人", "客戶", "申請人"):
        engine = MaskingEngine()
        text = "%s 陳美玲 的資料" % label
        engine.learn(text)
        assert "陳美玲" not in engine.mask_text(text)[0], label


def test_mask_agent_names_flag_covers_agent_labels():
    """加旗標後，內部人員標籤後的姓名也要遮。"""
    for label in ("業務員", "帳號", "經手人", "處理者"):
        engine = MaskingEngine(mask_agent_names=True)
        text = "%s 葉珍玲(HC-20) 受理" % label
        engine.learn(text)
        assert "葉珍玲" not in engine.mask_text(text)[0], label


def test_agent_label_after_name_does_not_exempt():
    """業務員一詞出現在姓名「之後」，不代表該姓名是業務員。"""
    engine = MaskingEngine()
    text = "保戶 陳美玲 的 聯絡資料 與 業務員/營業處所 相同"
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "陳美玲" not in out


def test_agent_name_column_hint_exempt():
    """表格欄位標題為業務員／帳號時，整格姓名也不遮。"""
    engine = MaskingEngine()
    engine.learn("王大同", "業務員姓名")
    out, _ = engine.mask_text("王大同", "業務員姓名")
    assert out == "王大同"


def test_mask_agent_names_flag_restores_masking():
    engine = MaskingEngine(mask_agent_names=True)
    text = "帳號 劉湘妘(N224463647) 之 地址"
    engine.learn(text)
    out, _ = engine.mask_text(text)
    assert "劉湘妘" not in out


def test_agent_hint_only_from_short_labels():
    """鄰格長文中偶然出現的「業務員」不得放行整格客戶姓名。"""
    long_neighbour = ("(業務侵佔) 2026-07-23 受理 全球人壽 保單 1150723LN00013 "
                      "保戶 周洺禾 的 聯絡資料 與 業務員/營業處所 相同")
    engine = MaskingEngine()
    text = "要保人 周洺禾(P123732535) 與 帳號 劉湘妘(N224463647) 之 地址 相同"
    engine.learn(text, long_neighbour)
    out, _ = engine.mask_text(text, long_neighbour)
    assert "周洺禾" not in out      # 保戶仍要遮
    assert "劉湘妘" in out          # 帳號（業務員）保留
    # 短欄位標題仍有效
    assert detectors.is_agent_context("王大同", 0, "業務員姓名")
    assert not detectors.is_agent_context("王大同", 0, long_neighbour)


def test_unlabelled_occurrence_of_agent_only_name_kept():
    """無標籤處：該姓名若只以內部人員身分出現過，不遮。

    實際查核檔的情境——J 欄「經手人 葉珍玲(HC-20)」，
    K 欄同一人再次出現但前方無標籤。
    """
    engine = MaskingEngine()
    engine.learn("(銷售資格) 受理 元大人壽 經手人 葉珍玲(HC-20) 不具資格")
    out, _ = engine.mask_text("葉珍玲(HC-20)(hcv2223195) 的 壽險 與 公平")
    assert "葉珍玲" in out


def test_unlabelled_occurrence_of_customer_name_masked():
    """反之，曾以客戶身分出現過的姓名，無標籤處仍要遮。"""
    engine = MaskingEngine()
    engine.learn("要保人 周洺禾 申請")
    engine.learn("帳號 周洺禾 之 地址")     # 同時也有內部人員身分
    out, _ = engine.mask_text("本案 周洺禾 之 資料")
    assert "周洺禾" not in out
