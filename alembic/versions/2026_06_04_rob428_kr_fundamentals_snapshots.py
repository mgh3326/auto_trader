"""Add KR fundamentals screener snapshot table for ROB-428 PR-A.

Additive (non-destructive): creates ``invest_kr_fundamentals_snapshots`` to
back the KR ``/invest/screener`` with tvscreener-sourced valuation +
fundamentals + sector/industry. Mirrors ``invest_crypto_screener_snapshots``.

Revision ID: 20260604_rob428
Revises: rob422_rob423_merge_heads
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260604_rob428"
down_revision = "rob422_rob423_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invest_kr_fundamentals_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("price", sa.Numeric(24, 8), nullable=True),
        sa.Column("change_rate", sa.Numeric(18, 6), nullable=True),
        sa.Column("volume", sa.Numeric(28, 4), nullable=True),
        sa.Column("market_cap", sa.Numeric(28, 4), nullable=True),
        sa.Column("per", sa.Numeric(18, 6), nullable=True),
        sa.Column("pbr", sa.Numeric(18, 6), nullable=True),
        sa.Column("dividend_yield", sa.Numeric(18, 6), nullable=True),
        sa.Column("roe_ttm", sa.Numeric(18, 6), nullable=True),
        sa.Column("payout_ratio_ttm", sa.Numeric(18, 6), nullable=True),
        sa.Column("gross_margin_ttm", sa.Numeric(18, 6), nullable=True),
        sa.Column("revenue_yoy", sa.Numeric(18, 6), nullable=True),
        sa.Column("eps_yoy", sa.Numeric(18, 6), nullable=True),
        sa.Column("eps_qoq", sa.Numeric(18, 6), nullable=True),
        sa.Column("net_income_yoy", sa.Numeric(18, 6), nullable=True),
        sa.Column("net_income_cagr_5y", sa.Numeric(18, 6), nullable=True),
        sa.Column("continuous_dividend_payout", sa.Numeric(10, 2), nullable=True),
        sa.Column("continuous_dividend_growth", sa.Numeric(10, 2), nullable=True),
        sa.Column("week_high_52", sa.Numeric(24, 8), nullable=True),
        sa.Column("rsi14", sa.Numeric(18, 6), nullable=True),
        sa.Column("sector", sa.String(length=120), nullable=True),
        sa.Column("industry", sa.String(length=120), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "computed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol",
            "snapshot_date",
            name="uq_invest_kr_fundamentals_snapshots_symbol_date",
        ),
        sa.CheckConstraint(
            "source IN ('tvscreener_kr')",
            name="ck_invest_kr_fundamentals_snapshots_source",
        ),
    )
    op.create_index(
        "ix_invest_kr_fundamentals_snapshots_date",
        "invest_kr_fundamentals_snapshots",
        ["snapshot_date"],
    )
    op.create_index(
        "ix_invest_kr_fundamentals_snapshots_date_roe",
        "invest_kr_fundamentals_snapshots",
        ["snapshot_date", "roe_ttm"],
    )
    op.create_index(
        "ix_invest_kr_fundamentals_snapshots_date_per",
        "invest_kr_fundamentals_snapshots",
        ["snapshot_date", "per"],
    )
    op.create_index(
        "ix_invest_kr_fundamentals_snapshots_date_dividend_yield",
        "invest_kr_fundamentals_snapshots",
        ["snapshot_date", "dividend_yield"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invest_kr_fundamentals_snapshots_date_dividend_yield",
        table_name="invest_kr_fundamentals_snapshots",
    )
    op.drop_index(
        "ix_invest_kr_fundamentals_snapshots_date_per",
        table_name="invest_kr_fundamentals_snapshots",
    )
    op.drop_index(
        "ix_invest_kr_fundamentals_snapshots_date_roe",
        table_name="invest_kr_fundamentals_snapshots",
    )
    op.drop_index(
        "ix_invest_kr_fundamentals_snapshots_date",
        table_name="invest_kr_fundamentals_snapshots",
    )
    op.drop_table("invest_kr_fundamentals_snapshots")
