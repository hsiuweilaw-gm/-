"""名單比對。

## 為什麼不能用單純的字串包含比對

匯入完整制裁名單後，可比對名稱達五萬筆以上，其中近千筆長度在四個字元以內
（例如 Joe、Adam、IZO、陳世憲）。若採雙向包含比對，客戶姓名「陳」會命中
名單上的「陳世憲」，姓名含 Adam 者會命中名單上的 Adam——結果是幾乎每位客戶
都被標紅。警示疲乏會讓真正的命中被忽略，比不做還糟。

## 本模組的作法

比對方向固定為「**名單上的名稱出現在客戶姓名之中**」，永不反向。
作法是由客戶姓名（很短）產生候選字串，再以索引查詢名單（很長），
而非載入整份名單逐筆掃描：

  1. 整串正規化後的姓名           → 完全相符
  2. 詞彙子集的排序鍵             → 拉丁字母姓名的詞序差異（CHEN SHIH-HSIEN / SHIH-HSIEN CHEN）
  3. 連續子字串（長度下限見下）   → 名單名稱嵌在較長的公司名稱中

長度下限用以排除短名稱造成的誤報：純漢字子字串至少 3 字，其餘至少 6 字元。
此下限只作用於「子字串」，客戶姓名整串一律做完全比對，因此客戶就叫 Adam 時仍會命中。

## 命中強度

  exact   客戶姓名整串等於名單上的名稱      → 應婉拒建立業務關係
  partial 名單名稱為客戶姓名的一部分        → 強制高風險，須人工複核

分級的理由：範本第四點要求婉拒的是「對象為制裁名單所列者」，
部分相符尚未構成該認定，逕行擋件會誤傷；但也不能放行。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import WatchListEntry, WatchListName

LIST_TYPES = {
    "sanction": "制裁名單",
    "terrorist": "資恐名單",
    "pep": "重要政治性職務人士",
    "high_risk_country": "高風險國家／地區",
}

# 以姓名比對的名單類別。高風險國家不列入：拿國名去比對客戶姓名沒有意義，
# 國別風險由問卷的地域與國籍因子及強制勾選題涵蓋。
SCREENED_LIST_TYPES = ("sanction", "terrorist", "pep")

# 命中即應婉拒的名單類別（範本第四點第八款）
BLOCKING_LIST_TYPES = ("sanction", "terrorist")

MIN_CJK_SUBSTRING = 3      # 純漢字子字串長度下限
MIN_OTHER_SUBSTRING = 6    # 其餘子字串長度下限
MAX_QUERY_LENGTH = 80      # 超過此長度的姓名不做子字串展開，避免候選數爆炸
MAX_TOKENS = 6             # 參與子集展開的詞彙數上限


def _is_cjk(ch: str) -> bool:
    return "㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿"


def normalize(value: str) -> str:
    """正規化：全形轉半形、轉大寫、去除所有非字母數字與非漢字之字元。"""
    folded = unicodedata.normalize("NFKC", value or "").upper()
    return "".join(ch for ch in folded if ch.isalnum() or _is_cjk(ch))


def tokenize(value: str) -> list[str]:
    """切出詞彙。漢字不含空白，會成為單一詞彙。"""
    folded = unicodedata.normalize("NFKC", value or "").upper()
    tokens, current = [], []
    for ch in folded:
        if ch.isalnum() or _is_cjk(ch):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def sort_key(value: str) -> str:
    """詞彙排序後串接，用於比對詞序不同的姓名。"""
    return "".join(sorted(tokenize(value)))


def _substring_candidates(flat: str) -> set[str]:
    """由（很短的）客戶姓名產生所有夠長的連續子字串。"""
    if len(flat) > MAX_QUERY_LENGTH:
        return set()
    out: set[str] = set()
    for start in range(len(flat)):
        for end in range(start + MIN_CJK_SUBSTRING, len(flat) + 1):
            piece = flat[start:end]
            if piece == flat:
                continue  # 整串另以完全比對處理
            minimum = MIN_CJK_SUBSTRING if all(_is_cjk(c) for c in piece) else MIN_OTHER_SUBSTRING
            if len(piece) >= minimum:
                out.add(piece)
    return out


def _subset_keys(tokens: list[str]) -> set[str]:
    """詞彙子集的排序鍵。用於名單名稱是客戶姓名詞彙之真子集的情形。"""
    tokens = tokens[:MAX_TOKENS]
    out: set[str] = set()
    for size in range(2, len(tokens)):
        for combo in combinations(sorted(tokens), size):
            out.add("".join(combo))
    return out


@dataclass(frozen=True)
class Hit:
    list_type: str
    list_label: str
    matched_value: str      # 命中的名單名稱
    entry_name: str         # 名單對象的主要名稱
    source: str | None
    external_id: str | None
    program: str | None
    status: str | None
    query: str              # 命中的欄位（要保人／被保險人／受益人）
    query_value: str        # 該欄位的內容
    confidence: str         # exact / partial

    @property
    def blocking(self) -> bool:
        return self.confidence == "exact" and self.list_type in BLOCKING_LIST_TYPES

    @property
    def describe(self) -> str:
        parts = [f"{self.list_label}"]
        if self.source:
            parts.append(self.source)
        if self.external_id:
            parts.append(self.external_id)
        tail = f"（{'／'.join(parts)}）"
        level = "完全相符" if self.confidence == "exact" else "部分相符，須人工複核"
        return f"{self.query}「{self.query_value}」{level}：{self.entry_name}{tail}"


def screen(db: Session, terms: dict[str, str]) -> list[Hit]:
    """以多個姓名欄位比對名單。terms 形如 {"要保人": "王大明", ...}。"""
    cleaned = {label: v.strip() for label, v in terms.items() if v and v.strip()}
    if not cleaned:
        return []

    # 為所有欄位一次收集候選，只查一次資料庫。
    exact_map: dict[str, list[tuple[str, str]]] = {}
    partial_map: dict[str, list[tuple[str, str]]] = {}
    for label, value in cleaned.items():
        flat = normalize(value)
        if not flat:
            continue
        key = sort_key(value)
        for candidate in {flat, key}:
            exact_map.setdefault(candidate, []).append((label, value))
        for candidate in _substring_candidates(flat) | _subset_keys(tokenize(value)):
            if candidate not in exact_map:
                partial_map.setdefault(candidate, []).append((label, value))

    all_candidates = set(exact_map) | set(partial_map)
    if not all_candidates:
        return []

    rows = db.execute(
        select(WatchListName)
        .join(WatchListEntry)
        .options(selectinload(WatchListName.entry))
        .where(
            WatchListEntry.active.is_(True),
            WatchListEntry.list_type.in_(SCREENED_LIST_TYPES),
            (WatchListName.normalized.in_(all_candidates))
            | (WatchListName.sort_key.in_(all_candidates)),
        )
    ).scalars().all()

    hits: list[Hit] = []
    seen: set[tuple] = set()
    for row in rows:
        entry = row.entry
        for candidate in {row.normalized, row.sort_key}:
            for label, value in exact_map.get(candidate, []):
                confidence = "exact"
                key = (entry.id, row.id, label, confidence)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(_build(entry, row, label, value, confidence))
            for label, value in partial_map.get(candidate, []):
                key = (entry.id, row.id, label, "partial")
                if key in seen or (entry.id, row.id, label, "exact") in seen:
                    continue
                seen.add(key)
                hits.append(_build(entry, row, label, value, "partial"))
    # 完全相符排在前面，供畫面優先呈現。
    hits.sort(key=lambda h: (h.confidence != "exact", h.list_type, h.entry_name))
    return hits


def _build(entry: WatchListEntry, row: WatchListName, label: str, value: str,
           confidence: str) -> Hit:
    return Hit(
        list_type=entry.list_type,
        list_label=LIST_TYPES.get(entry.list_type, entry.list_type),
        matched_value=row.name,
        entry_name=entry.name_zh or entry.value,
        source=entry.source,
        external_id=entry.external_id,
        program=entry.program,
        status=entry.status,
        query=label,
        query_value=value,
        confidence=confidence,
    )


def names_for(value: str) -> list[tuple[str, str, str]]:
    """由一個名稱產生 (name, normalized, sort_key)；正規化後為空則捨棄。"""
    flat = normalize(value)
    if not flat:
        return []
    return [(value.strip(), flat, sort_key(value))]


def add_entry(db: Session, list_type: str, value: str, *, source: str | None = None,
              note: str | None = None, external_id: str | None = None,
              name_zh: str | None = None, aliases: list[str] | None = None,
              entity_type: str | None = None, countries: str | None = None,
              program: str | None = None, listed_on: str | None = None,
              status: str | None = None, batch: str | None = None) -> WatchListEntry | None:
    """新增一筆名單對象及其所有可比對名稱。已存在（同來源同編號）則更新。"""
    existing = None
    if external_id:
        existing = db.query(WatchListEntry).filter(
            WatchListEntry.list_type == list_type,
            WatchListEntry.source == source,
            WatchListEntry.external_id == external_id,
        ).one_or_none()
    entry = existing or WatchListEntry(list_type=list_type, source=source,
                                       external_id=external_id)
    entry.value = value.strip()
    entry.name_zh = (name_zh or "").strip() or None
    entry.entity_type = entity_type
    entry.countries = countries
    entry.program = program
    entry.listed_on = listed_on
    entry.status = status
    entry.note = note
    entry.batch = batch
    # 已除名者停用，但保留紀錄以證明曾經比對過。
    entry.active = (status or "").strip() != "已除名"
    if existing:
        entry.names.clear()
    db.add(entry)

    seen: set[str] = set()
    variants = [(value, "primary"), (name_zh, "zh")] + [(a, "alias") for a in (aliases or [])]
    for raw, kind in variants:
        if not raw:
            continue
        for name, flat, key in names_for(str(raw)):
            if flat in seen:
                continue
            seen.add(flat)
            entry.names.append(
                WatchListName(name=name[:512], kind=kind, normalized=flat[:512],
                              sort_key=key[:512])
            )
    return entry if entry.names else None


def upsert(db: Session, list_type: str, value: str, source: str | None = None,
           note: str | None = None) -> WatchListEntry | None:
    """洗防人員手動新增單筆名單。"""
    normalized = normalize(value)
    if not normalized:
        return None
    existing = (
        db.query(WatchListEntry)
        .join(WatchListName)
        .filter(WatchListEntry.list_type == list_type,
                WatchListEntry.source.is_(None) | (WatchListEntry.source == source),
                WatchListName.normalized == normalized)
        .first()
    )
    if existing:
        existing.value = value.strip()
        existing.source = source
        existing.note = note
        existing.active = True
        return existing
    return add_entry(db, list_type, value, source=source, note=note, batch="manual")
