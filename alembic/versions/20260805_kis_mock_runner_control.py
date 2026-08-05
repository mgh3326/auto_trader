"""Add the durable KR-B0 KIS mock runner kill-switch control row.

This migration is additive: it creates one new review-schema table and seeds
its only permitted row as ACTIVE.  Applying it to an operator database is a
separate KR-B2 deployment action; KR-B0 tests use only pytest-owned databases.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260805_kis_mock_runner"
down_revision: str | Sequence[str] | None = "20260804_alpaca_clean_account"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kis_mock_runner_control"
_SCHEMA = "review"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_kis_mock_runner_control_singleton"),
        sa.CheckConstraint(
            "mode IN ('ACTIVE','ENTRY_HALT','GLOBAL_FREEZE')",
            name="ck_kis_mock_runner_control_mode",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kis_mock_runner_control"),
        schema=_SCHEMA,
    )
    op.execute(
        "INSERT INTO review.kis_mock_runner_control "
        "(id, mode, reason, updated_by) "
        "VALUES (1, 'ACTIVE', 'initial_control_row', 'migration:KR-B0')"
    )


def downgrade() -> None:
    op.drop_table(_TABLE, schema=_SCHEMA)
