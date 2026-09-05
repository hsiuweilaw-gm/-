"""watchlist hit review

Revision ID: 1fe2f9cbbca4
Revises: 373902b9c6cc
Create Date: 2026-09-05 10:35:39.367913+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '1fe2f9cbbca4'
down_revision: str | None = '373902b9c6cc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_assessments_hit_cleared_by_id_users"


def upgrade() -> None:
    with op.batch_alter_table("assessments", schema=None) as batch_op:
        # 既有案件一律視為未曾命中制裁名單，故補 server_default 後再移除，
        # 否則 PostgreSQL 會因既有資料列無值而拒絕加上 NOT NULL 欄位。
        batch_op.add_column(sa.Column("watchlist_hit_sanction", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("hit_cleared_at", sa.DateTime(timezone=True),
                                      nullable=True))
        batch_op.add_column(sa.Column("hit_cleared_by_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("hit_cleared_note", sa.Text(), nullable=True))
        batch_op.create_foreign_key(FK_NAME, "users", ["hit_cleared_by_id"], ["id"])

    with op.batch_alter_table("assessments", schema=None) as batch_op:
        batch_op.alter_column("watchlist_hit_sanction", server_default=None)

    # PostgreSQL 的 assessmentstatus 是資料庫層級的具名列舉型別，新增狀態必須
    # 明確加上標籤；SQLite 把 Enum 存成 VARCHAR，沒有型別可加，此步驟不適用。
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE assessmentstatus ADD VALUE IF NOT EXISTS 'HIT_REVIEW'")


def downgrade() -> None:
    # PostgreSQL 無法自列舉型別移除標籤。降版後若仍有 HIT_REVIEW 狀態的案件，
    # 舊版程式無法解讀，故先將其歸回待主管同意——資料不滅，僅回到前一版的語意。
    op.execute(
        "UPDATE assessments SET status = 'PENDING_APPROVAL' WHERE status = 'HIT_REVIEW'"
    )
    with op.batch_alter_table("assessments", schema=None) as batch_op:
        batch_op.drop_constraint(FK_NAME, type_="foreignkey")
        batch_op.drop_column("hit_cleared_note")
        batch_op.drop_column("hit_cleared_by_id")
        batch_op.drop_column("hit_cleared_at")
        batch_op.drop_column("watchlist_hit_sanction")
