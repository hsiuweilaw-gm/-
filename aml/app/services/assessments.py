"""評估案件的建立、自動儲存、計分與送出。

自動儲存是本系統的硬性需求：業務員在客戶面前填寫，中途離線或關閉頁面
都不得遺失已輸入資料，且每一次變更都要進稽核軌跡。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    Answer,
    Assessment,
    AssessmentStatus,
    RiskLevel,
    User,
    utcnow,
)
from ..scoring.engine import (
    Questionnaire,
    ScoreResult,
    load_questionnaire,
    score_assessment,
)
from ..security import blind_index, decrypt_pii, encrypt_pii
from . import audit, screening
from .screening import Hit

# 範本第六點：確認客戶身分紀錄、契約文件保存至業務關係結束後至少 5 年。
# 業務關係結束日在本系統無法得知，故以保單年度屆滿後保守推估，
# 實際清理仍需人工複核，系統僅提供到期提示、不自動刪除。
RETENTION_YEARS = 7

CHECK_GROUPS = frozenset({"refusal", "mandatory", "suspicious"})


def next_case_no(db: Session) -> str:
    """案號格式：AML-YYYYMM-####（月流水）。"""
    today = date.today()
    prefix = f"AML-{today:%Y%m}-"
    latest = (
        db.query(func.max(Assessment.case_no))
        .filter(Assessment.case_no.like(f"{prefix}%"))
        .scalar()
    )
    seq = int(latest.rsplit("-", 1)[1]) + 1 if latest else 1
    return f"{prefix}{seq:04d}"


def create_draft(db: Session, agent: User, ip: str | None = None) -> Assessment:
    q = load_questionnaire()
    assessment = Assessment(
        case_no=next_case_no(db),
        agent_id=agent.id,
        org_unit_id=agent.org_unit_id,
        questionnaire_id=q.id,
        questionnaire_version=q.version,
        status=AssessmentStatus.DRAFT,
    )
    db.add(assessment)
    db.flush()
    audit.record(
        db, actor=agent, action="assessment.create", entity_type="assessment",
        entity_id=assessment.case_no, detail={"questionnaire": q.key}, ip=ip,
    )
    db.commit()
    return assessment


def questionnaire_for(assessment: Assessment) -> Questionnaire:
    return load_questionnaire(assessment.questionnaire_id, assessment.questionnaire_version)


def answers_map(assessment: Assessment) -> dict[str, str]:
    return {a.factor_code: a.option_code for a in assessment.answers}


def stored_checks(assessment: Assessment) -> dict[str, list[str]]:
    """讀出三組勾選題的作答碼。"""
    if not assessment.checks_json:
        return {}
    try:
        data = json.loads(assessment.checks_json)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def evaluate(assessment: Assessment) -> ScoreResult:
    """僅依作答計算，不含名單比對。供不需要資料庫的情境使用。"""
    q = questionnaire_for(assessment)
    stored = stored_checks(assessment)
    return score_assessment(
        q,
        answers_map(assessment),
        refusal_checks=set(stored.get("refusal", [])),
        mandatory_checks=set(stored.get("mandatory", [])),
        suspicious_checks=set(stored.get("suspicious", [])),
    )


def screen_case(db: Session, assessment: Assessment) -> list[Hit]:
    """以要保人、被保險人與受益人姓名比對制裁、資恐、PEP 與高風險國家名單。

    比對永遠使用當下的名單，不快取結果 —— 名單更新後，舊案件重新開啟時
    仍會反映新名單，這是持續監控的基本要求。
    """
    return screening.screen(db, {
        "要保人": decrypt_pii(assessment.holder_name_enc) or "",
        "被保險人": decrypt_pii(assessment.insured_name_enc) or "",
        "受益人": decrypt_pii(assessment.beneficiary_name_enc) or "",
    })


# 命中這兩類名單即應婉拒建立業務關係（範本第四點第八款）；
# 其餘名單類別（PEP、高風險國家）則強制列為高風險（範本第五點第一、二款）。
BLOCKING_LISTS = ("sanction", "terrorist")


def evaluate_with_screening(db: Session, assessment: Assessment) -> tuple[ScoreResult, list[Hit]]:
    """作答評分 + 名單比對。這是所有對外呈現與送出判定應使用的版本。

    名單比對的結果只會提升風險等級或擋件，不會降低，也不會更動總分。
    """
    result = evaluate(assessment)
    hits = screen_case(db, assessment)
    for hit in hits:
        description = f"{hit.list_label}命中：{hit.query}「{hit.matched_value}」"
        if hit.list_type in BLOCKING_LISTS:
            result.blocked_reasons.append(f"應婉拒建立業務關係：{description}")
            result.blocked = True
        else:
            result.override_reasons.append(f"強制高風險：{description}")
            result.override_applied = True
    if hits:
        result.level = "high"
    return result, hits


def apply_result(assessment: Assessment, result: ScoreResult) -> None:
    assessment.total_score = result.total_score
    assessment.risk_level = RiskLevel.HIGH if result.level == "high" else RiskLevel.GENERAL
    assessment.override_applied = result.override_applied
    assessment.override_reasons = (
        json.dumps(result.override_reasons, ensure_ascii=False) if result.override_reasons else None
    )
    assessment.blocked_reasons = (
        json.dumps(result.blocked_reasons, ensure_ascii=False) if result.blocked_reasons else None
    )
    assessment.offshore_remittance = bool(
        {"offshore_remittance", "oiu_policy"} & result.flags
    )


def save_answer(
    db: Session, assessment: Assessment, actor: User, factor_code: str, option_code: str,
    ip: str | None = None,
) -> ScoreResult:
    """儲存單題作答並即時重算。每一次變更都留痕，含前後值。"""
    q = questionnaire_for(assessment)
    factor = q.factor(factor_code)
    if factor is None:
        raise ValueError(f"未知的風險因子：{factor_code}")
    option = factor.option(option_code)
    if option is None:
        raise ValueError(f"未知的選項：{factor_code}/{option_code}")

    existing = next((a for a in assessment.answers if a.factor_code == factor_code), None)
    previous = existing.option_code if existing else None
    previous_score = existing.score if existing else None

    if existing:
        existing.option_code = option.code
        existing.score = option.score
    else:
        db.add(
            Answer(
                assessment_id=assessment.id,
                factor_code=factor.code,
                option_code=option.code,
                score=option.score,
            )
        )
    db.flush()
    db.refresh(assessment)

    result, _ = evaluate_with_screening(db, assessment)
    apply_result(assessment, result)

    audit.record(
        db, actor=actor, action="assessment.answer", entity_type="assessment",
        entity_id=assessment.case_no,
        detail={
            "factor": factor.code,
            "factor_label": factor.label,
            "from": previous,
            "from_score": previous_score,
            "to": option.code,
            "to_score": option.score,
            "total_after": result.total_score,
            "level_after": result.level,
        },
        ip=ip,
    )
    db.commit()
    return result


def save_checks(
    db: Session, assessment: Assessment, actor: User, group: str, codes: list[str],
    ip: str | None = None,
) -> ScoreResult:
    if group not in CHECK_GROUPS:
        raise ValueError(f"未知的勾選群組：{group}")
    stored = stored_checks(assessment)
    previous = stored.get(group, [])
    stored[group] = sorted(set(codes))
    assessment.checks_json = json.dumps(stored, ensure_ascii=False)
    db.flush()

    result, _ = evaluate_with_screening(db, assessment)
    apply_result(assessment, result)
    audit.record(
        db, actor=actor, action="assessment.checks", entity_type="assessment",
        entity_id=assessment.case_no,
        detail={"group": group, "from": previous, "to": stored[group],
                "level_after": result.level, "blocked": result.blocked},
        ip=ip,
    )
    db.commit()
    return result


def save_profile(
    db: Session, assessment: Assessment, actor: User, data: dict[str, Any],
    ip: str | None = None,
) -> ScoreResult:
    """儲存客戶基本資料與保單資訊。個資欄位加密，稽核軌跡不記錄明文。"""
    changed: list[str] = []

    def set_enc(attr: str, value: str | None) -> None:
        if value is None:
            return
        setattr(assessment, attr, encrypt_pii(value.strip()) if value.strip() else None)
        changed.append(attr)

    set_enc("holder_name_enc", data.get("holder_name"))
    set_enc("insured_name_enc", data.get("insured_name"))
    set_enc("beneficiary_name_enc", data.get("beneficiary_name"))

    holder_id = data.get("holder_id")
    if holder_id is not None:
        holder_id = holder_id.strip()
        assessment.holder_id_enc = encrypt_pii(holder_id) if holder_id else None
        assessment.holder_id_bidx = blind_index(holder_id) if holder_id else None
        changed.append("holder_id")

    for attr in ("insurer_name", "policy_no", "wealth_source", "fund_source_detail"):
        if attr in data and data[attr] is not None:
            setattr(assessment, attr, data[attr].strip() or None)
            changed.append(attr)

    if data.get("annual_premium") not in (None, ""):
        try:
            assessment.annual_premium = float(str(data["annual_premium"]).replace(",", ""))
            changed.append("annual_premium")
        except ValueError as exc:
            raise ValueError("保費金額格式錯誤") from exc

    db.flush()
    # 姓名可能剛被填入或修改，須立即重新比對名單並更新風險等級。
    result, hits = evaluate_with_screening(db, assessment)
    apply_result(assessment, result)

    audit.record(
        db, actor=actor, action="assessment.profile", entity_type="assessment",
        entity_id=assessment.case_no,
        detail={
            "fields": changed,
            "watchlist_hits": [
                {"list": h.list_type, "matched": h.matched_value, "field": h.query} for h in hits
            ],
            "consulted": assessment.consulted_name,
        },
        ip=ip,
    )
    db.commit()
    return result


def record_consultation(
    db: Session, assessment: Assessment, actor: User, supervisor_name: str,
    ip: str | None = None,
) -> None:
    """記錄業務員已照會單位主管。

    業務員看不到分數，但系統在跨越門檻時會警示；此照會確認是送出高風險案件的前提，
    對應內控手冊「確認客戶風險等級為高風險時，應立即通知主管備查及列管」之要求。
    """
    supervisor_name = supervisor_name.strip()
    if not supervisor_name:
        raise ValueError("請填寫照會之主管姓名")
    assessment.consulted_supervisor = True
    assessment.consulted_name = supervisor_name
    assessment.consulted_at = utcnow()
    audit.record(
        db, actor=actor, action="assessment.consulted", entity_type="assessment",
        entity_id=assessment.case_no, detail={"supervisor": supervisor_name}, ip=ip,
    )
    db.commit()


def submit(db: Session, assessment: Assessment, actor: User, ip: str | None = None) -> ScoreResult:
    """送出評估。

    - 未填完：拒絕送出。
    - 命中婉拒事由：狀態 BLOCKED，通知專責主管，業務員不得續辦。
    - 高風險：須先完成照會主管之紀錄，狀態 PENDING_APPROVAL，經主管同意始得建立業務關係。
    - 一般風險：狀態 SUBMITTED，直接完成。
    """
    result, hits = evaluate_with_screening(db, assessment)
    if not result.complete:
        q = questionnaire_for(assessment)
        labels = [q.factor(c).label for c in result.missing_factors if q.factor(c)]
        raise ValueError("尚有風險因子未填答：" + "、".join(labels))
    if result.level == "high" and not result.blocked and not assessment.consulted_supervisor:
        raise ValueError("本案須先照會單位主管並於畫面確認後，始得送出")

    apply_result(assessment, result)
    if result.blocked:
        assessment.status = AssessmentStatus.BLOCKED
    elif result.level == "high":
        assessment.status = AssessmentStatus.PENDING_APPROVAL
    else:
        assessment.status = AssessmentStatus.SUBMITTED

    assessment.submitted_at = utcnow()
    assessment.retain_until = date.today() + timedelta(days=365 * RETENTION_YEARS)

    audit.record(
        db, actor=actor, action="assessment.submit", entity_type="assessment",
        entity_id=assessment.case_no,
        detail={
            "total_score": result.total_score,
            "level": result.level,
            "status": assessment.status.value,
            "override_reasons": result.override_reasons,
            "blocked_reasons": result.blocked_reasons,
            "watchlist_hits": [
                {"list": h.list_type, "matched": h.matched_value, "field": h.query} for h in hits
            ],
            "consulted": assessment.consulted_name,
        },
        ip=ip,
    )
    db.commit()
    return result
