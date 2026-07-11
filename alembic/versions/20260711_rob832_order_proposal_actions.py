"""ROB-832 order proposal action columns (review schema, additive).

Revision ID: 20260711_rob832_actions
Revises: 20260711_rob816_exit_binding
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260711_rob832_actions"
down_revision: str | Sequence[str] | None = "20260711_rob816_exit_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "order_proposals",
        sa.Column("action", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "order_proposals",
        sa.Column("target_broker_order_id", sa.Text(), nullable=True),
        schema="review",
    )
    op.create_check_constraint(
        "order_proposals_action",
        "order_proposals",
        "action IS NULL OR action IN ('place','replace','cancel')",
        schema="review",
    )


def downgrade() -> None:
    op.drop_constraint(
        "order_proposals_action", "order_proposals", type_="check", schema="review"
    )
    op.drop_column("order_proposals", "target_broker_order_id", schema="review")
    op.drop_column("order_proposals", "action", schema="review")
