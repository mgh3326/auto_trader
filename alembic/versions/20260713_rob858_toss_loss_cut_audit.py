"""ROB-858 Toss loss-cut audit binding.

Revision ID: 20260713_rob858_toss_loss_cut
Revises: 20260712_rob846_experiments
Create Date: 2026-07-13

Additive-only audit fields for accepted Toss live orders. Existing rows remain
valid with NULL values; loss-cut sends persist the exact exit intent,
retrospective, and Paperclip approval issue used at execution time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260713_rob858_toss_loss_cut"
down_revision: str | Sequence[str] | None = "20260712_rob846_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("exit_intent", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("retrospective_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.add_column(
        "toss_live_order_ledger",
        sa.Column("approval_issue_id", sa.Text(), nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column("toss_live_order_ledger", "approval_issue_id", schema="review")
    op.drop_column("toss_live_order_ledger", "retrospective_id", schema="review")
    op.drop_column("toss_live_order_ledger", "exit_intent", schema="review")
