"""add financial_fundamentals_snapshots (ROB-422 PR1)

Revision ID: rob422_fin_fundamentals
Revises: 20260602_rob412_main_merge
Create Date: 2026-06-02 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "rob422_fin_fundamentals"
down_revision: str | None = "20260602_rob412_main_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "financial_fundamentals_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("fiscal_period", sa.String(length=10), nullable=False),
        sa.Column("period_type", sa.String(length=10), nullable=False),
        sa.Column("period_end_date", sa.Date(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=True),
        sa.Column("effective_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("source_collected_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("revenue", sa.Numeric(30, 2), nullable=True),
        sa.Column("net_income", sa.Numeric(30, 2), nullable=True),
        sa.Column("gross_profit", sa.Numeric(30, 2), nullable=True),
        sa.Column("cost_of_sales", sa.Numeric(30, 2), nullable=True),
        sa.Column("roe", sa.Numeric(20, 4), nullable=True),
        sa.Column("payout_ratio", sa.Numeric(10, 6), nullable=True),
        sa.Column("dividend_per_share", sa.Numeric(20, 4), nullable=True),
        sa.Column("discrete_revenue", sa.Numeric(30, 2), nullable=True),
        sa.Column("discrete_net_income", sa.Numeric(30, 2), nullable=True),
        sa.Column(
            "data_state",
            sa.String(length=12),
            server_default="fresh",
            nullable=False,
        ),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "schema_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "computed_at",
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
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "market IN ('kr', 'us')",
            name="ck_financial_fundamentals_snapshots_market",
        ),
        sa.CheckConstraint(
            "period_type IN ('annual', 'quarterly')",
            name="ck_financial_fundamentals_snapshots_period_type",
        ),
        sa.CheckConstraint(
            "source IN ('dart')",
            name="ck_financial_fundamentals_snapshots_source",
        ),
        sa.CheckConstraint(
            "data_state IN ('fresh', 'stale', 'partial', 'unavailable')",
            name="ck_financial_fundamentals_snapshots_data_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "fiscal_period",
            "source",
            name="uq_financial_fundamentals_snapshots_msfs",
        ),
    )
    op.create_index(
        "ix_financial_fundamentals_snapshots_market_symbol_period_end",
        "financial_fundamentals_snapshots",
        ["market", "symbol", "period_end_date"],
    )
    op.create_index(
        "ix_financial_fundamentals_snapshots_market_symbol_filing",
        "financial_fundamentals_snapshots",
        ["market", "symbol", "filing_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_fundamentals_snapshots_market_symbol_filing",
        table_name="financial_fundamentals_snapshots",
    )
    op.drop_index(
        "ix_financial_fundamentals_snapshots_market_symbol_period_end",
        table_name="financial_fundamentals_snapshots",
    )
    op.drop_table("financial_fundamentals_snapshots")
