"""名單篩檢。

比對制裁名單、資恐名單、PEP 與高風險國家名單。名單由洗防專責人員在
後台維護（內控手冊 BIC06-03 八(八)：持續留意國際組織發布之訊息）。

比對採「正規化後包含比對」：去除空白、全形轉半形、英文轉大寫。
這是刻意保守的作法 — 寧可誤報由專責人員排除，不可漏報。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import WatchListEntry

LIST_TYPES = {
    "sanction": "制裁名單",
    "terrorist": "資恐名單",
    "pep": "重要政治性職務人士",
    "high_risk_country": "高風險國家／地區",
}


def normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value or "")
    return "".join(folded.split()).upper()


@dataclass
class Hit:
    list_type: str
    list_label: str
    matched_value: str
    source: str | None
    query: str


def screen(db: Session, terms: dict[str, str]) -> list[Hit]:
    """以多個名詞（要保人、被保險人、受益人、實質受益人、國別等）比對名單。"""
    normalized_terms = {label: normalize(v) for label, v in terms.items() if v and v.strip()}
    if not normalized_terms:
        return []

    hits: list[Hit] = []
    entries = db.query(WatchListEntry).filter(WatchListEntry.active.is_(True)).all()
    for entry in entries:
        for label, term in normalized_terms.items():
            if not entry.normalized_value:
                continue
            if entry.normalized_value in term or term in entry.normalized_value:
                hits.append(
                    Hit(
                        list_type=entry.list_type,
                        list_label=LIST_TYPES.get(entry.list_type, entry.list_type),
                        matched_value=entry.value,
                        source=entry.source,
                        query=label,
                    )
                )
    return hits


def upsert(db: Session, list_type: str, value: str, source: str | None = None,
           note: str | None = None) -> WatchListEntry:
    normalized = normalize(value)
    entry = (
        db.query(WatchListEntry)
        .filter(WatchListEntry.list_type == list_type,
                WatchListEntry.normalized_value == normalized)
        .one_or_none()
    )
    if entry:
        entry.value = value
        entry.source = source
        entry.note = note
        entry.active = True
        return entry
    entry = WatchListEntry(
        list_type=list_type, value=value, normalized_value=normalized, source=source, note=note
    )
    db.add(entry)
    return entry
