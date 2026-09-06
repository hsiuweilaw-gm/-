"""two factor auth

Revision ID: 45fc0c7f5f62
Revises: 1fe2f9cbbca4
Create Date: 2026-09-05 12:32:25.852422+00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '45fc0c7f5f62'
down_revision: str | None = '1fe2f9cbbca4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 三欄皆可為空：既有帳號在下次登入時才會被要求設定。
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret_enc', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('totp_confirmed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('totp_last_counter', sa.Integer(), nullable=True))


def downgrade() -> None:
    # 降版會刪除已登錄的密鑰，使用者須重新設定驗證器 App。
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('totp_last_counter')
        batch_op.drop_column('totp_confirmed_at')
        batch_op.drop_column('totp_secret_enc')
