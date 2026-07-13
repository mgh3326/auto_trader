"""ROB-859 add an unscored terminal status for no-claim forecasts.

Revision ID: 20260713_rob859_no_claim
Revises: 20260712_rob846_experiments
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260713_rob859_no_claim"
down_revision: str | Sequence[str] | None = "20260712_rob846_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_trade_forecasts_status",
        "trade_forecasts",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_forecasts_status",
        "trade_forecasts",
        "status IN ('open','closed','closed_no_claim')",
        schema="review",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE review.trade_forecasts "
        "SET status = 'closed' "
        "WHERE status = 'closed_no_claim'"
    )
    op.drop_constraint(
        "ck_trade_forecasts_status",
        "trade_forecasts",
        schema="review",
        type_="check",
    )
    op.create_check_constraint(
        "ck_trade_forecasts_status",
        "trade_forecasts",
        "status IN ('open','closed')",
        schema="review",
    )
