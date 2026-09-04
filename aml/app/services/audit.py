"""稽核軌跡。

範本第六點要求紀錄足以「重建個別交易」；內控手冊 BIC06-03 亦將洗錢防制
列為稽核重點查核項目。本模組是唯一的寫入點，且系統不提供任何修改或
刪除稽核事件的介面。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditEvent, User


def record(
    db: Session,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor.id if actor else None,
        actor_name=actor.display_name if actor else None,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=json.dumps(detail, ensure_ascii=False) if detail else None,
        ip=ip,
    )
    db.add(event)
    return event


def trail(db: Session, entity_type: str, entity_id: str | int) -> list[AuditEvent]:
    return (
        db.query(AuditEvent)
        .filter(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == str(entity_id))
        .order_by(AuditEvent.at.asc(), AuditEvent.id.asc())
        .all()
    )


def parse_detail(event: AuditEvent) -> dict[str, Any]:
    if not event.detail:
        return {}
    try:
        return json.loads(event.detail)
    except json.JSONDecodeError:
        return {}
