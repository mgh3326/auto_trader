"""ROB-440 PR3: add high_52w_date to market_valuation_snapshots.

Additive (nullable): the date the 52-week high occurred, sourced from yfinance
daily OHLC (max High date) for US. Powers US undervalued_breakout date-recency
parity (a NEW 52-week high within ~20 XNYS trading days — matching the KR/Toss
"신고가 경신" definition, ROB-432) instead of the price-proximity proxy. KR rows
leave it NULL (KR display uses the tvscreener week_high_52_date path).

Revision ID: 20260605_rob440
Revises: 20260605_rob441
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260605_rob440"
down_revision = "20260605_rob441"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "market_valuation_snapshots",
        sa.Column("high_52w_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_valuation_snapshots", "high_52w_date")
