"""業務員名冊批次匯入。

300 人以上規模不可能逐一建帳號，且初始密碼須逐人交付，
故匯入後一併產出可下載的帳密清單。

匯入採「先全部驗證、再全部寫入」：任何一列有誤即整批不寫入，
避免名冊匯到一半、無從判斷哪些已建立。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..models import OrgUnit, Role, User
from ..security import hash_password, new_token

# 欄位名稱同時接受中文與英文，避免使用者被欄位命名卡住。
COLUMNS = {
    "username": ("帳號", "username"),
    "display_name": ("姓名", "display_name", "name"),
    "role": ("角色", "role"),
    "org_code": ("單位代碼", "org_code", "org_unit"),
    "license_no": ("業務員登錄字號", "登錄字號", "license_no"),
    "license_valid_until": ("登錄有效日期", "license_valid_until"),
    "training_date": ("洗防訓練完訓日", "訓練完訓日", "training_date"),
}
REQUIRED = ("username", "display_name")

TEMPLATE_HEADER = [
    "帳號", "姓名", "角色", "單位代碼", "業務員登錄字號", "登錄有效日期", "洗防訓練完訓日",
]
TEMPLATE_SAMPLE = [
    ["agent001", "王大明", "agent", "TP01", "經登字第123456號", "2027-06-30", "2026-03-15"],
    ["sup_tp", "台北通訊處經理", "supervisor", "TP01", "", "", "2026-03-15"],
]


@dataclass
class RowError:
    line: int
    message: str


@dataclass
class Credential:
    username: str
    display_name: str
    password: str


@dataclass
class ImportResult:
    created: list[Credential] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def template_csv() -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADER)
    writer.writerows(TEMPLATE_SAMPLE)
    return buffer.getvalue()


def credentials_csv(credentials: list[Credential]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["帳號", "姓名", "初始密碼"])
    for c in credentials:
        writer.writerow([c.username, c.display_name, c.password])
    return buffer.getvalue()


def _normalize_header(raw: str) -> str | None:
    cleaned = (raw or "").strip().lstrip("﻿").lower()
    for key, aliases in COLUMNS.items():
        if cleaned in {a.lower() for a in aliases}:
            return key
    return None


def _parse_date(value: str, line: int, field_label: str, errors: list[RowError]) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(RowError(line, f"{field_label}「{value}」格式錯誤，應為 YYYY-MM-DD"))
        return None


def parse_and_import(db: Session, content: str) -> ImportResult:
    """解析 CSV 並匯入。全部驗證通過才寫入資料庫。"""
    result = ImportResult()

    # Excel 另存的 CSV 常帶 BOM，且 Windows 換行需正規化。
    text = content.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.errors.append(RowError(0, "檔案是空的"))
        return result

    mapping: dict[int, str] = {}
    for index, raw in enumerate(header):
        key = _normalize_header(raw)
        if key:
            mapping[index] = key
    missing = [k for k in REQUIRED if k not in mapping.values()]
    if missing:
        labels = "、".join(COLUMNS[k][0] for k in missing)
        result.errors.append(RowError(1, f"缺少必要欄位：{labels}"))
        return result

    units = {u.code: u for u in db.query(OrgUnit).all()}
    existing = {u.username: u for u in db.query(User).all()}
    seen: set[str] = set()
    pending_new: list[tuple[User, str]] = []
    pending_update: list[User] = []

    for line, row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in row):
            continue
        values = {key: (row[i].strip() if i < len(row) else "") for i, key in mapping.items()}

        username = values.get("username", "")
        display_name = values.get("display_name", "")
        if not username or not display_name:
            result.errors.append(RowError(line, "帳號與姓名不得留白"))
            continue
        if username in seen:
            result.errors.append(RowError(line, f"帳號「{username}」在檔案中重複"))
            continue
        seen.add(username)

        role_raw = values.get("role", "") or "agent"
        try:
            role = Role(role_raw)
        except ValueError:
            allowed = "、".join(r.value for r in Role)
            result.errors.append(RowError(line, f"角色「{role_raw}」無效，可用值：{allowed}"))
            continue

        org_code = values.get("org_code", "")
        unit = None
        if org_code:
            unit = units.get(org_code)
            if unit is None:
                result.errors.append(
                    RowError(line, f"單位代碼「{org_code}」不存在，請先於系統管理建立該單位")
                )
                continue

        license_valid = _parse_date(
            values.get("license_valid_until", ""), line, "登錄有效日期", result.errors
        )
        training = _parse_date(
            values.get("training_date", ""), line, "洗防訓練完訓日", result.errors
        )

        user = existing.get(username)
        if user:
            # 既有帳號只更新資料，不重設密碼，避免匯入名冊把在職人員鎖在外面。
            user.display_name = display_name
            user.role = role
            user.org_unit_id = unit.id if unit else user.org_unit_id
            user.agent_license_no = values.get("license_no") or user.agent_license_no
            user.license_valid_until = license_valid or user.license_valid_until
            user.aml_training_date = training or user.aml_training_date
            pending_update.append(user)
        else:
            password = new_token()[:16]
            pending_new.append((
                User(
                    username=username,
                    display_name=display_name,
                    password_hash=hash_password(password),
                    role=role,
                    org_unit_id=unit.id if unit else None,
                    agent_license_no=values.get("license_no") or None,
                    license_valid_until=license_valid,
                    aml_training_date=training,
                    must_change_password=True,
                ),
                password,
            ))

    if result.errors:
        db.rollback()
        return result

    for user, password in pending_new:
        db.add(user)
        result.created.append(
            Credential(username=user.username, display_name=user.display_name, password=password)
        )
    result.updated = [u.username for u in pending_update]
    return result
