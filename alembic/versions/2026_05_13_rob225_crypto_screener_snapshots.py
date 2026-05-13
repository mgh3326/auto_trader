"""Add crypto screener snapshot table for ROB-225.

Revision ID: 20260513_rob225
Revises: 20260513_rob227
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260513_rob225"
down_revision = "20260513_rob227"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invest_crypto_screener_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("latest_close", sa.Numeric(24, 8), nullable=False),
        sa.Column("change_amount", sa.Numeric(24, 8), nullable=True),
        sa.Column("change_rate", sa.Numeric(10, 4), nullable=True),
        sa.Column("trade_amount_24h", sa.Numeric(28, 4), nullable=True),
        sa.Column("volume_24h", sa.Numeric(28, 8), nullable=True),
        sa.Column("volume_24h_usd", sa.Numeric(28, 4), nullable=True),
        sa.Column("market_cap", sa.Numeric(28, 4), nullable=True),
        sa.Column("rsi", sa.Numeric(10, 4), nullable=True),
        sa.Column("adx", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "market_warning",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
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
            name="uq_invest_crypto_screener_snapshots_symbol_date",
        ),
        sa.CheckConstraint(
            "symbol LIKE 'KRW-%'",
            name="ck_invest_crypto_screener_snapshots_symbol",
        ),
        sa.CheckConstraint(
            "source IN ('tvscreener_upbit')",
            name="ck_invest_crypto_screener_snapshots_source",
        ),
    )
    op.create_index(
        "ix_invest_crypto_screener_snapshots_date",
        "invest_crypto_screener_snapshots",
        ["snapshot_date"],
    )
    op.create_index(
        "ix_invest_crypto_screener_snapshots_date_trade_amount",
        "invest_crypto_screener_snapshots",
        ["snapshot_date", "trade_amount_24h"],
    )
    op.create_index(
        "ix_invest_crypto_screener_snapshots_date_rsi",
        "invest_crypto_screener_snapshots",
        ["snapshot_date", "rsi"],
    )
    op.create_index(
        "ix_invest_crypto_screener_snapshots_date_change_rate",
        "invest_crypto_screener_snapshots",
        ["snapshot_date", "change_rate"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invest_crypto_screener_snapshots_date_change_rate",
        table_name="invest_crypto_screener_snapshots",
    )
    op.drop_index(
        "ix_invest_crypto_screener_snapshots_date_rsi",
        table_name="invest_crypto_screener_snapshots",
    )
    op.drop_index(
        "ix_invest_crypto_screener_snapshots_date_trade_amount",
        table_name="invest_crypto_screener_snapshots",
    )
    op.drop_index(
        "ix_invest_crypto_screener_snapshots_date",
        table_name="invest_crypto_screener_snapshots",
    )
    op.drop_table("invest_crypto_screener_snapshots")
