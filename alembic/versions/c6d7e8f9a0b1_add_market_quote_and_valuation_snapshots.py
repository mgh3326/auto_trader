"""add market quote and valuation snapshots

Revision ID: c6d7e8f9a0b1
Revises: 9f1a2b3c4d5e
Create Date: 2026-05-12 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "9f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_quote_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("snapshot_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(20, 6), nullable=False),
        sa.Column("previous_close", sa.Numeric(20, 6), nullable=True),
        sa.Column("open", sa.Numeric(20, 6), nullable=True),
        sa.Column("high", sa.Numeric(20, 6), nullable=True),
        sa.Column("low", sa.Numeric(20, 6), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
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
            "market IN ('kr', 'us', 'crypto')", name="ck_market_quote_snapshots_market"
        ),
        sa.CheckConstraint(
            "source IN ('kis', 'yahoo', 'upbit', 'naver_finance')",
            name="ck_market_quote_snapshots_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "source",
            "snapshot_at",
            name="uq_market_quote_snapshots_market_symbol_source_at",
        ),
    )
    op.create_index(
        "ix_market_quote_snapshots_market_symbol_at",
        "market_quote_snapshots",
        ["market", "symbol", "snapshot_at"],
    )

    op.create_table(
        "market_valuation_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("per", sa.Numeric(20, 4), nullable=True),
        sa.Column("pbr", sa.Numeric(20, 4), nullable=True),
        sa.Column("roe", sa.Numeric(20, 4), nullable=True),
        sa.Column("dividend_yield", sa.Numeric(10, 6), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 2), nullable=True),
        sa.Column("high_52w", sa.Numeric(20, 6), nullable=True),
        sa.Column("low_52w", sa.Numeric(20, 6), nullable=True),
        sa.Column(
            "raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True
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
            "market IN ('kr', 'us')", name="ck_market_valuation_snapshots_market"
        ),
        sa.CheckConstraint(
            "source IN ('naver_finance', 'yahoo')",
            name="ck_market_valuation_snapshots_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market",
            "symbol",
            "snapshot_date",
            "source",
            name="uq_market_valuation_snapshots_market_symbol_date_source",
        ),
    )
    op.create_index(
        "ix_market_valuation_snapshots_market_date",
        "market_valuation_snapshots",
        ["market", "snapshot_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_valuation_snapshots_market_date",
        table_name="market_valuation_snapshots",
    )
    op.drop_table("market_valuation_snapshots")
    op.drop_index(
        "ix_market_quote_snapshots_market_symbol_at",
        table_name="market_quote_snapshots",
    )
    op.drop_table("market_quote_snapshots")
