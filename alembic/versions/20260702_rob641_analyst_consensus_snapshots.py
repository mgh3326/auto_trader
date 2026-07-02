"""add analyst_consensus_snapshots (ROB-641)

Revision ID: 20260702_rob641
Revises: 20260620_scalp_benchmark
Create Date: 2026-07-02

New snapshot table for analyst consensus data (buy/hold/sell counts, target
prices). Clones the market_quote_snapshots / market_valuation_snapshots pattern
but lives in the ``review`` schema and keys on ``snapshot_date``.
KR source: naver_finance. US source: yfinance.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260702_rob641"
down_revision: str | Sequence[str] | None = "20260620_scalp_benchmark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyst_consensus_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("buy_count", sa.Integer(), nullable=True),
        sa.Column("hold_count", sa.Integer(), nullable=True),
        sa.Column("sell_count", sa.Integer(), nullable=True),
        sa.Column("strong_buy_count", sa.Integer(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True),
        sa.Column("target_mean", sa.Numeric(20, 4), nullable=True),
        sa.Column("target_median", sa.Numeric(20, 4), nullable=True),
        sa.Column("target_high", sa.Numeric(20, 4), nullable=True),
        sa.Column("target_low", sa.Numeric(20, 4), nullable=True),
        sa.Column("upside_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("analyst_count", sa.Integer(), nullable=True),
        sa.Column("newest_opinion_date", sa.Date(), nullable=True),
        sa.Column("current_price", sa.Numeric(20, 4), nullable=True),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "collected_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us')", name="ck_analyst_consensus_snapshots_market"
        ),
        sa.CheckConstraint(
            "source IN ('naver_finance', 'yfinance')",
            name="ck_analyst_consensus_snapshots_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_analyst_consensus_snapshots_market_symbol_date_source",
        ),
        schema="review",
    )
    op.create_index(
        "ix_analyst_consensus_snapshots_market_symbol_date",
        "analyst_consensus_snapshots",
        ["market", "symbol", sa.text("snapshot_date DESC")],
        schema="review",
    )
    op.create_index(
        "ix_analyst_consensus_snapshots_market_date",
        "analyst_consensus_snapshots",
        ["market", sa.text("snapshot_date DESC")],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_analyst_consensus_snapshots_market_date",
        table_name="analyst_consensus_snapshots",
        schema="review",
    )
    op.drop_index(
        "ix_analyst_consensus_snapshots_market_symbol_date",
        table_name="analyst_consensus_snapshots",
        schema="review",
    )
    op.drop_table("analyst_consensus_snapshots", schema="review")
