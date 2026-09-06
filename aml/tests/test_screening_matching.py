"""名單比對演算法測試。

匯入完整制裁名單後可比對名稱達五萬筆以上，其中近千筆長度在四字元以內。
比對太鬆會讓幾乎每位客戶都被標紅，警示疲乏後真正的命中反而被忽略；
比對太嚴則漏掉該擋的人。以下把兩端的行為都鎖住。
"""
from __future__ import annotations

import pytest

from app.services import screening


@pytest.fixture
def listed(db):
    """建立一份縮小但具代表性的名單：含短名、中文名、公司名與別名。"""
    screening.add_entry(db, "sanction", "CHEN SHIH-HSIEN", source="TW",
                        external_id="TW-107001", name_zh="陳世憲")
    screening.add_entry(db, "sanction", "Bunker's Taiwan Group Corporation", source="TW",
                        external_id="TW-107002", name_zh="邦克台灣集團公司")
    screening.add_entry(db, "sanction", "Adam", source="OFAC", external_id="OFAC-1")
    screening.add_entry(db, "sanction", "KIM CHOL SAM", source="UN", external_id="KPi.035",
                        name_zh="金铁三", aliases=["Jin Tiesan"])
    screening.add_entry(db, "pep", "某政治人物", source="手動", external_id="PEP-1")
    screening.add_entry(db, "sanction", "已除名的人", source="TW", external_id="TW-000",
                        status="已除名")
    db.commit()


def hits_for(db, name):
    return screening.screen(db, {"要保人": name})


# ---------------------------------------------------------------- 應命中

def test_exact_chinese_name_is_an_exact_hit(db, listed):
    hits = hits_for(db, "陳世憲")
    assert len(hits) == 1
    assert hits[0].confidence == "exact"
    assert hits[0].blocking is True, "制裁名單完全相符應擋件"


def test_exact_latin_name_matches_regardless_of_word_order(db, listed):
    """CHEN SHIH-HSIEN 與 SHIH-HSIEN CHEN 是同一人，詞序不應影響比對。"""
    for name in ("CHEN SHIH-HSIEN", "Shih-Hsien Chen", "chen  shih hsien"):
        hits = hits_for(db, name)
        assert hits and hits[0].confidence == "exact", name


def test_alias_is_matched(db, listed):
    hits = hits_for(db, "Jin Tiesan")
    assert hits and hits[0].entry_name == "金铁三"


def test_customer_named_exactly_adam_is_flagged(db, listed):
    """名單上就是短名時，客戶整串等於它仍應命中——長度下限只作用於子字串。"""
    hits = hits_for(db, "Adam")
    assert hits and hits[0].confidence == "exact"


def test_company_name_embedded_in_longer_string_is_partial(db, listed):
    hits = hits_for(db, "邦克台灣集團公司股份有限公司")
    assert hits, "名單名稱嵌在較長公司名稱中應被發現"
    assert hits[0].confidence == "partial"
    assert hits[0].blocking is False, "部分相符不逕行擋件，交人工複核"


def test_pep_hit_is_not_blocking(db, listed):
    hits = hits_for(db, "某政治人物")
    assert hits and hits[0].list_type == "pep"
    assert hits[0].blocking is False


def test_all_three_name_fields_are_screened(db, listed):
    hits = screening.screen(db, {"要保人": "王大明", "被保險人": "李小華",
                                 "受益人": "陳世憲"})
    assert len(hits) == 1
    assert hits[0].query == "受益人"


# ---------------------------------------------------------------- 不應命中

def test_single_character_surname_does_not_match_a_longer_listed_name(db, listed):
    """這是舊版雙向包含比對最嚴重的缺陷：客戶姓「陳」命中名單上的「陳世憲」。"""
    assert hits_for(db, "陳") == []


def test_partial_chinese_name_does_not_match(db, listed):
    assert hits_for(db, "陳世") == []
    assert hits_for(db, "世憲") == []


def test_common_name_containing_a_short_listed_name_is_not_flagged(db, listed):
    """名單上有 Adam，但客戶叫 Adam Chen 不應因此被擋——否則全公司客戶都會被標紅。"""
    assert hits_for(db, "Adam Chen") == []
    assert hits_for(db, "ADAMSON") == []


def test_unrelated_names_produce_nothing(db, listed):
    for name in ("王大明", "林志豪", "John Smith", "台灣人壽保險股份有限公司"):
        assert hits_for(db, name) == [], name


def test_delisted_entry_is_not_matched(db, listed):
    """已除名者不得再觸發警示，但紀錄保留以證明曾經比對。"""
    assert hits_for(db, "已除名的人") == []
    from app.models import WatchListEntry
    entry = db.query(WatchListEntry).filter(WatchListEntry.external_id == "TW-000").one()
    assert entry.active is False


def test_empty_and_whitespace_terms_are_ignored(db, listed):
    assert screening.screen(db, {"要保人": "", "被保險人": "   "}) == []


def test_high_risk_country_list_is_not_used_for_name_matching(db):
    """拿國名比對客戶姓名沒有意義，國別風險由問卷因子涵蓋。"""
    screening.add_entry(db, "high_risk_country", "緬甸", source="金管會函轉")
    db.commit()
    assert screening.screen(db, {"要保人": "緬甸"}) == []


# ---------------------------------------------------------------- 正規化

def test_normalization_handles_width_case_punctuation(db, listed):
    for name in ("ＣＨＥＮ　ＳＨＩＨ－ＨＳＩＥＮ", "chen, shih-hsien", "CHEN SHIH HSIEN"):
        assert hits_for(db, name), name


def test_very_long_name_does_not_explode(db, listed):
    """超長字串不做子字串展開，避免候選數爆炸拖垮儲存。"""
    hits = hits_for(db, "台" * 200)
    assert hits == []


def test_overlong_source_fields_do_not_break_the_import(db):
    """外部名單的欄位長度無法預期，過長時須截斷而非讓整批匯入失敗。

    SQLite 不強制 VARCHAR 長度，PostgreSQL 會直接拒絕寫入——
    不先截斷的話，某次名單更新會在正式環境炸掉，而開發環境完全看不出來。
    本次匯入的來源檔中，別名欄位最長就有 3,008 字元。
    """
    entry = screening.add_entry(
        db, "sanction", "X" * 900,
        source="S" * 200, external_id="E" * 200, name_zh="中" * 900,
        entity_type="T" * 100, countries="C" * 900, program="P" * 900,
        listed_on="D" * 100, status="U" * 100, batch="B" * 200,
        aliases=["A" * 900],
    )
    db.commit()
    assert len(entry.value) <= 512
    assert len(entry.name_zh) <= 512
    assert len(entry.countries) <= 512
    assert len(entry.program) <= 512
    assert len(entry.source) <= 64
    assert len(entry.external_id) <= 64
    assert len(entry.entity_type) <= 32
    assert len(entry.status) <= 32
    assert len(entry.batch) <= 64
    assert all(len(n.name) <= 512 and len(n.normalized) <= 512 for n in entry.names)
