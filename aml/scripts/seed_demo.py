"""建立示範資料，供上線前流程確認與教育訓練使用。

⚠️ 僅供測試環境。正式環境請勿執行，且務必使用不同的資料庫。
用法：  python -m scripts.seed_demo
"""
from __future__ import annotations

import random
import sys

from app.db import SessionLocal, init_db
from app.models import OrgUnit, Role, User
from app.scoring.engine import load_questionnaire
from app.security import hash_password
from app.services import assessments as svc
from app.services import screening

DEMO_PASSWORD = "demo-password-1234"

UNITS = [("TP01", "台北通訊處"), ("TC01", "台中通訊處"), ("KH01", "高雄通訊處")]
STAFF = [
    ("admin", "系統管理者", Role.ADMIN, None),
    ("aml01", "洗防專責主管", Role.COMPLIANCE, None),
    ("audit01", "內部稽核", Role.AUDITOR, None),
    ("sup_tp", "台北通訊處經理", Role.SUPERVISOR, "TP01"),
    ("sup_tc", "台中通訊處經理", Role.SUPERVISOR, "TC01"),
    ("sup_kh", "高雄通訊處經理", Role.SUPERVISOR, "KH01"),
]
AGENTS = [(f"agent{i:02d}", f"業務員{i:02d}", UNITS[i % 3][0]) for i in range(1, 13)]

WATCHLIST = [
    ("high_risk_country", ["緬甸", "北韓", "伊朗", "Myanmar", "DPRK", "Iran"],
     "金管會函轉 FATF 公告（示範資料，正式環境請以實際函令為準）"),
    ("sanction", ["示範制裁對象股份有限公司"], "示範資料"),
]


def main() -> None:
    init_db()
    db = SessionLocal()

    if db.query(User).count():
        print("資料庫已有帳號，為避免覆蓋既有資料，示範資料未寫入。")
        sys.exit(1)

    units = {}
    for code, name in UNITS:
        unit = OrgUnit(code=code, name=name)
        db.add(unit)
        units[code] = unit
    db.flush()

    for username, display, role, unit_code in STAFF:
        db.add(User(username=username, display_name=display,
                    password_hash=hash_password(DEMO_PASSWORD), role=role,
                    org_unit_id=units[unit_code].id if unit_code else None,
                    must_change_password=False))
    agents = []
    for username, display, unit_code in AGENTS:
        user = User(username=username, display_name=display,
                    password_hash=hash_password(DEMO_PASSWORD), role=Role.AGENT,
                    org_unit_id=units[unit_code].id, must_change_password=False,
                    agent_license_no=f"經登字第{random.randint(100000, 999999)}號")
        db.add(user)
        agents.append(user)
    for list_type, values, source in WATCHLIST:
        for value in values:
            screening.upsert(db, list_type, value, source)
    db.commit()

    q = load_questionnaire()
    rng = random.Random(20260604)
    # 真實分布：多數為一般風險，少數高風險，個位數擋件。
    weights = {f.code: [max(1, 6 - o.score) for o in f.options] for f in q.factors}

    for n in range(60):
        agent = rng.choice(agents)
        case = svc.create_draft(db, agent)
        for factor in q.factors:
            option = rng.choices(factor.options, weights=weights[factor.code])[0]
            svc.save_answer(db, case, agent, factor.code, option.code)
        svc.save_profile(db, case, agent, {
            "holder_name": f"示範客戶{n:03d}",
            "holder_id": f"A1{rng.randint(10000000, 99999999)}",
            "insurer_name": rng.choice(["示範人壽", "範例人壽", "測試人壽"]),
            "policy_no": f"D{rng.randint(1000000, 9999999)}",
            "annual_premium": str(rng.choice([60_000, 120_000, 480_000, 1_500_000, 6_000_000])),
        })
        if n % 20 == 0:
            svc.save_checks(db, case, agent, "mandatory", ["pep"])
        if n == 7:
            svc.save_checks(db, case, agent, "refusal", ["sanction_list_hit"])
        if n == 11:
            svc.save_checks(db, case, agent, "suspicious", ["A2"])
        if n % 15 != 14:  # 保留少數草稿
            # 高風險案件須先照會主管才能送出
            if svc.evaluate(case).level == "high":
                svc.record_consultation(db, case, agent, "通訊處經理")
            svc.submit(db, case, agent)

    total = db.query(User).count()
    print(f"示範資料已建立：{total} 個帳號、60 件評估案件。")
    print(f"所有帳號的密碼皆為：{DEMO_PASSWORD}")
    print("建議登入：admin / aml01（洗防專責）/ sup_tp（主管）/ agent01（業務員）")
    db.close()


if __name__ == "__main__":
    main()
