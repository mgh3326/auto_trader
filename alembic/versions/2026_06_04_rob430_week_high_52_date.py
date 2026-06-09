"""Add week_high_52_date to invest_kr_fundamentals_snapshots for ROB-430 PR-②.

Additive (non-destructive): adds a nullable ``week_high_52_date`` column so the
``undervalued_breakout`` screener preset can match Toss's "신고가" = a NEW 52-week
high made within ~20 days (a breakout event), instead of the price/52w-high
proximity proxy. Sourced from tvscreener ``PRICE_52_WEEK_HIGH_DATE``.

Revision ID: 20260604_rob430
Revises: 20260604_rob428
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260604_rob430"
down_revision = "20260604_rob428"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invest_kr_fundamentals_snapshots",
        sa.Column("week_high_52_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invest_kr_fundamentals_snapshots", "week_high_52_date")
