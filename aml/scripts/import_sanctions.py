"""由檔案匯入制裁名單。

用法：
    python -m scripts.import_sanctions 名單.xlsx
    python -m scripts.import_sanctions 名單.csv --batch 202609

供每月例行更新使用；亦可由後台「名單管理」頁上傳。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from app.db import SessionLocal, assert_schema_current
from app.services import sanctions_import


def main() -> None:
    parser = argparse.ArgumentParser(description="匯入制裁名單")
    parser.add_argument("path", help="xlsx 或 csv 檔案路徑")
    parser.add_argument("--batch", default=None, help="批次標記，預設為當下時間")
    parser.add_argument("--sheet", default=None, help="xlsx 工作表名稱，預設「全部清單」")
    parser.add_argument("--keep-old", action="store_true",
                        help="不停用同來源的舊資料（預設會停用，以反映名單移除）")
    args = parser.parse_args()

    path = pathlib.Path(args.path)
    if not path.exists():
        sys.exit(f"找不到檔案：{path}")

    assert_schema_current()
    db = SessionLocal()
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        rows = sanctions_import.rows_from_xlsx(path.read_bytes(), args.sheet)
    else:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp950")
        rows = sanctions_import.rows_from_csv(text)

    result = sanctions_import.import_rows(
        db, rows, batch=args.batch, replace_sources=not args.keep_old
    )
    if not result.ok:
        db.rollback()
        for message in result.errors:
            print("錯誤：", message)
        sys.exit(1)
    db.commit()

    print(f"批次 {result.batch}")
    print(f"  匯入對象 {result.entries} 筆，可比對名稱 {result.names} 筆")
    for source, count in sorted(result.by_source.items()):
        print(f"    {source}: {count}")
    if result.skipped:
        print(f"  略過（無有效名稱）{result.skipped} 筆")
    if result.deactivated:
        print(f"  停用同來源舊資料 {result.deactivated} 筆")
    db.close()


if __name__ == "__main__":
    main()
