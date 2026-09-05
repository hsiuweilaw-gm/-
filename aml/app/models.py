"""資料模型。

保存年限依保經公司防制洗錢範本第六點：交易紀錄至少 5 年，
確認客戶身分紀錄、契約文件、業務往來資訊保存至業務關係結束後至少 5 年。
本系統一律不實體刪除評估案件，僅以狀態標記，以確保稽核軌跡完整。
"""
from __future__ import annotations

import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """把資料庫取回的時間補上時區。

    SQLite 不保存時區，取回來是 naive；PostgreSQL 則帶時區。
    直接拿來與 utcnow() 比較會在 SQLite 上拋 TypeError，
    因此凡是與現在時刻比較的地方都要先經過這裡。
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    """角色對應內控三道防線。"""

    AGENT = "agent"              # 業務人員（第一道防線）
    SUPERVISOR = "supervisor"    # 通訊處／營運中心主管（第一道防線督導主管）
    COMPLIANCE = "compliance"    # 防制洗錢及打擊資恐專責主管／人員（第二道防線）
    AUDITOR = "auditor"          # 內部稽核（第三道防線，唯讀）
    ADMIN = "admin"              # 系統管理者


class AssessmentStatus(str, enum.Enum):
    DRAFT = "draft"              # 業務員填寫中（自動儲存）
    SUBMITTED = "submitted"      # 已送出，一般風險案件即完成
    PENDING_APPROVAL = "pending_approval"  # 高風險，待主管同意（範本第五點第一款第一目）
    APPROVED = "approved"        # 主管同意建立業務關係
    REJECTED = "rejected"        # 主管不同意
    BLOCKED = "blocked"          # 系統強制婉拒（範本第四點）
    # 曾命中制裁／資恐名單者，送出後一律先由洗防專責覆核，不直接完成。
    # 擋件依當下姓名判定，改名即解除；此關卡確保每一次命中都有人看過並留下結論。
    HIT_REVIEW = "hit_review"    # 待洗防覆核（曾命中制裁／資恐名單）
    CLOSED = "closed"            # 案件結案／作廢（保留紀錄）


class RiskLevel(str, enum.Enum):
    GENERAL = "general"          # 一般風險
    HIGH = "high"                # 高風險


class OrgUnit(Base):
    """通訊處／營運中心。300 人以上規模需以組織階層切分權限與報表。"""

    __tablename__ = "org_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent: Mapped[OrgUnit | None] = relationship(remote_side=[id])
    users: Mapped[list[User]] = relationship(back_populates="org_unit")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.AGENT)
    org_unit_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"))

    # 業務人員登錄字號。保經業務員須經登錄始得招攬，此欄位供勾稽登錄狀態。
    agent_license_no: Mapped[str | None] = mapped_column(String(64))
    license_valid_until: Mapped[date | None] = mapped_column(Date)
    # 洗錢防制教育訓練最近完訓日（範本第十三點；業務人員每年應受訓）
    aml_training_date: Mapped[date | None] = mapped_column(Date)

    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    org_unit: Mapped[OrgUnit | None] = relationship(back_populates="users")


class Assessment(Base):
    """一件客戶風險辨識評估（對應一張紙本檢核表）。"""

    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    agent_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    org_unit_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"), index=True)

    questionnaire_id: Mapped[str] = mapped_column(String(32))
    questionnaire_version: Mapped[int] = mapped_column(Integer)

    # 要保人資料。姓名與身分證字號加密儲存，另存盲索引供查詢。
    holder_name_enc: Mapped[str | None] = mapped_column(Text)
    holder_id_enc: Mapped[str | None] = mapped_column(Text)
    holder_id_bidx: Mapped[str | None] = mapped_column(String(64), index=True)
    insured_name_enc: Mapped[str | None] = mapped_column(Text)
    beneficiary_name_enc: Mapped[str | None] = mapped_column(Text)

    # 保單資訊（供年度報表以「件數」與「保費金額」雙重加權彙總）
    insurer_name: Mapped[str | None] = mapped_column(String(128))
    policy_no: Mapped[str | None] = mapped_column(String(64))
    annual_premium: Mapped[float | None] = mapped_column(Float)

    status: Mapped[AssessmentStatus] = mapped_column(
        Enum(AssessmentStatus), default=AssessmentStatus.DRAFT, index=True
    )
    total_score: Mapped[int | None] = mapped_column(Integer, index=True)
    risk_level: Mapped[RiskLevel | None] = mapped_column(Enum(RiskLevel), index=True)
    # 若等級係由強制規則提升／擋件而非總分決定，記錄其依據
    override_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reasons: Mapped[str | None] = mapped_column(Text)  # JSON list
    blocked_reasons: Mapped[str | None] = mapped_column(Text)   # JSON list

    # 名單命中留痕：一旦命中即記錄，之後不因業務員修改姓名而清除。
    # 若無此欄位，業務員看到「應婉拒」後只要放棄草稿改個寫法重填，
    # 洗防人員永遠不會知道曾經命中過。
    watchlist_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    watchlist_hit_note: Mapped[str | None] = mapped_column(Text)
    # 命中的是否為制裁／資恐名單（相對於 PEP）。此類命中送出後須經洗防專責覆核。
    watchlist_hit_sanction: Mapped[bool] = mapped_column(Boolean, default=False)
    # 洗防專責之覆核結論。留存於案件本身，金檢時無須翻稽核軌跡即可查得。
    hit_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hit_cleared_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    hit_cleared_note: Mapped[str | None] = mapped_column(Text)

    # 高風險案件之照會紀錄。業務員看不到分數，但跨越門檻時系統會警示，
    # 並要求其確認已照會單位主管後始得送出（內控手冊：確認為高風險時應立即通知主管備查及列管）。
    consulted_supervisor: Mapped[bool] = mapped_column(Boolean, default=False)
    consulted_name: Mapped[str | None] = mapped_column(String(64))
    consulted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 強化措施（範本第五點第一款；內控手冊 BIC06-03 八(二)）
    wealth_source: Mapped[str | None] = mapped_column(Text)     # 財富來源
    fund_source_detail: Mapped[str | None] = mapped_column(Text)  # 資金實質來源
    edd_note: Mapped[str | None] = mapped_column(Text)

    # 三組勾選題的作答碼：{"refusal": [...], "mandatory": [...], "suspicious": [...]}
    # 對應範本第四點婉拒事由、第五點強制高風險條件、附錄疑似洗錢態樣。
    checks_json: Mapped[str | None] = mapped_column(Text)

    # STR 申報（範本第九點）
    str_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    str_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    str_reference: Mapped[str | None] = mapped_column(String(64))

    # 境外電匯／OIU 保單（年度報表獨立統計欄位）
    offshore_remittance: Mapped[bool] = mapped_column(Boolean, default=False)

    # 定期審查（範本第五點第一款第三目「強化之持續監督」；問答集 Q8）
    review_due_on: Mapped[date | None] = mapped_column(Date, index=True)
    last_reviewed_on: Mapped[date | None] = mapped_column(Date)
    # 最近一次以當時名單重新篩檢的時間
    rescreened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    # 依範本第六點，紀錄保存期限屆滿日；到期前不得清理。
    retain_until: Mapped[date | None] = mapped_column(Date)

    agent: Mapped[User] = relationship(foreign_keys=[agent_id])
    hit_cleared_by: Mapped[User | None] = relationship(foreign_keys=[hit_cleared_by_id])
    answers: Mapped[list[Answer]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[Approval]] = relationship(back_populates="assessment")
    reviews: Mapped[list[PeriodicReview]] = relationship(
        back_populates="assessment", order_by="PeriodicReview.performed_at.desc()"
    )


class Answer(Base):
    """單題作答。保留每一題的分數快照，避免問卷改版後回溯不一致。"""

    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("assessment_id", "factor_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"), index=True)
    factor_code: Mapped[str] = mapped_column(String(64))
    option_code: Mapped[str] = mapped_column(String(64))
    score: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    assessment: Mapped[Assessment] = relationship(back_populates="answers")


class Approval(Base):
    """高風險案件之主管同意紀錄（範本第五點第一款第一目）。"""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"), index=True)
    approver_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approver_role: Mapped[Role] = mapped_column(Enum(Role))
    decision: Mapped[str] = mapped_column(String(16))  # approved / rejected
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assessment: Mapped[Assessment] = relationship(back_populates="approvals")
    approver: Mapped[User] = relationship()


class ReviewOutcome(str, enum.Enum):
    UNCHANGED = "unchanged"        # 維持原風險等級
    ESCALATED = "escalated"        # 調升為高風險
    DEESCALATED = "deescalated"    # 調降為一般風險
    REASSESS = "reassess"          # 應重新辦理客戶審查（資料已不足或有疑義）
    TERMINATED = "terminated"      # 終止業務關係


class PeriodicReview(Base):
    """既有客戶的定期審查紀錄。

    範本第五點第一款第三目要求對高風險客戶之業務往來關係採取強化之持續監督；
    問答集 Q8 要求定期檢視客戶及實質受益人身分資料是否足夠並確保更新。
    招攬當下評估一次不足以滿足這項要求，故每案於送出時排定下次應審查日，
    到期由洗防專責人員複核並記錄結論。
    """

    __tablename__ = "periodic_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"), index=True)
    due_on: Mapped[date] = mapped_column(Date)
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    performed_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    outcome: Mapped[ReviewOutcome] = mapped_column(Enum(ReviewOutcome))
    risk_level_before: Mapped[RiskLevel | None] = mapped_column(Enum(RiskLevel))
    risk_level_after: Mapped[RiskLevel | None] = mapped_column(Enum(RiskLevel))
    watchlist_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    next_due_on: Mapped[date | None] = mapped_column(Date)

    assessment: Mapped[Assessment] = relationship(back_populates="reviews")
    reviewer: Mapped[User] = relationship()


class AuditEvent(Base):
    """唯增稽核軌跡。任何寫入操作都必須留痕，且不提供修改與刪除介面。

    業務員在送出前反覆改答案並非違規，但「臨界值附近的反覆修改」是招攬端
    調降風險分數的典型徵候，故每一次作答變更都逐筆保存，供第二、三道防線檢核。
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    detail: Mapped[str | None] = mapped_column(Text)  # JSON
    ip: Mapped[str | None] = mapped_column(String(64))


Index("ix_audit_entity", AuditEvent.entity_type, AuditEvent.entity_id)


class WatchListEntry(Base):
    """名單上的一個對象（自然人、法人、船舶等）。

    一個對象可能有多個可比對名稱（原文、中文、別名），存於 WatchListName。
    """

    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("list_type", "source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    list_type: Mapped[str] = mapped_column(String(32), index=True)
    # sanction（制裁名單）/ terrorist（資恐）/ pep（重要政治性職務人士）/ high_risk_country
    value: Mapped[str] = mapped_column(String(512))          # 主要名稱，供顯示
    # 來源清單（TW／UN／OFAC／EU／手動）與該清單的編號
    source: Mapped[str | None] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(32))  # Person / Organization / Vessel …
    name_zh: Mapped[str | None] = mapped_column(String(512))
    countries: Mapped[str | None] = mapped_column(String(512))
    program: Mapped[str | None] = mapped_column(String(512))  # 依據／制裁計畫
    listed_on: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str | None] = mapped_column(String(32))  # 制裁有效／已除名
    note: Mapped[str | None] = mapped_column(Text)
    batch: Mapped[str | None] = mapped_column(String(64), index=True)  # 匯入批次
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    names: Mapped[list[WatchListName]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )


class WatchListName(Base):
    """名單對象的一個可比對名稱。

    比對一律以「候選字串查索引」進行，不做全表掃描：
    由客戶姓名產生候選（整串、詞彙子集的排序鍵、連續子字串），再以索引查詢。
    這確保比對方向永遠是「名單上的名稱出現在客戶姓名中」，
    而非反向——後者會讓客戶「陳」命中名單上的「陳世憲」。
    """

    __tablename__ = "watchlist_names"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("watchlist.id"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))  # primary / zh / alias
    # 正規化後去除所有非字母數字與非漢字之字元
    normalized: Mapped[str] = mapped_column(String(512), index=True)
    # 詞彙排序後串接，用於比對詞序不同的拉丁字母姓名
    sort_key: Mapped[str] = mapped_column(String(512), index=True)

    entry: Mapped[WatchListEntry] = relationship(back_populates="names")


Index("ix_watchlist_names_lookup", WatchListName.normalized, WatchListName.sort_key)


class ReportExport(Base):
    """報表匯出紀錄。金檢時需證明報表產出時點與產出人。"""

    __tablename__ = "report_exports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(32))  # board / annual / risk_assessment
    period_label: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    generated_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str | None] = mapped_column(String(64))

    generator: Mapped[User] = relationship()
