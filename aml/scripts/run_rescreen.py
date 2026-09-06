"""以當下的名單重新篩檢所有仍在監督中的案件。

名單每月更新，既有客戶可能在事後才被列入。建議於每次名單匯入後執行，
或由排程每月執行一次：

    python -m scripts.run_rescreen
"""
from __future__ import annotations

import argparse

from app.db import SessionLocal, assert_schema_current
from app.models import Role, User
from app.services import reviews


def main() -> None:
    parser = argparse.ArgumentParser(description="重新篩檢既有客戶")
    parser.add_argument("--limit", type=int, default=None, help="限制檢視件數（測試用）")
    parser.add_argument("--actor", default=None,
                        help="執行者帳號，供稽核軌跡記錄；省略時記為系統執行")
    args = parser.parse_args()

    assert_schema_current()
    db = SessionLocal()
    actor = None
    if args.actor:
        actor = db.query(User).filter(User.username == args.actor).one_or_none()
        if actor is None:
            raise SystemExit(f"找不到帳號：{args.actor}")
        if actor.role not in (Role.COMPLIANCE, Role.ADMIN):
            raise SystemExit("執行者須為洗防專責或系統管理者")

    result = reviews.rescreen(db, actor=actor, limit=args.limit)
    print(f"檢視 {result.checked} 件，新增命中 {result.hit_count} 件")
    for case in result.new_hits:
        print(f"  {case.case_no}  {case.watchlist_hit_note}")
    db.close()


if __name__ == "__main__":
    main()
