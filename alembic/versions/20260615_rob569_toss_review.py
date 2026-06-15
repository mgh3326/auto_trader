"""ROB-569 add Toss reconcile manual-review fields.

Revision ID: 20260615_rob569_toss_review
Revises: ec2fbbc5898c
Create Date: 2026-06-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260615_rob569_toss_review"
down_revision: str | Sequence[str] | None = "ec2fbbc5898c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "toss_live_order_ledger",
        sa.Column(
            "requires_manual_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("manual_review_reason", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column(
            "last_reconcile_error",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.create_index(
        "ix_toss_live_ledger_manual_review",
        "toss_live_order_ledger",
        ["requires_manual_review"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_toss_live_ledger_manual_review",
        table_name="toss_live_order_ledger",
        schema="review",
    )
    op.drop_column("toss_live_order_ledger", "last_reconcile_error", schema="review")
    op.drop_column("toss_live_order_ledger", "manual_review_reason", schema="review")
    op.drop_column("toss_live_order_ledger", "requires_manual_review", schema="review")
