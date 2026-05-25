"""rob313 scalp_trade_analytics table

Per-round-trip cost/analytics record for the Binance Demo scalping loop
(ROB-313). Additive only; the order lifecycle stays in
``binance_demo_order_ledger`` (untouched).

Revision ID: 20260525_rob313
Revises: 424459cba097
Create Date: 2026-05-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260525_rob313"
down_revision: str | Sequence[str] | None = "424459cba097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scalp_trade_analytics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("open_client_order_id", sa.Text(), nullable=False),
        sa.Column("close_client_order_id", sa.Text(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(28, 12), nullable=False),
        sa.Column("entry_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("exit_price", sa.Numeric(28, 12), nullable=True),
        sa.Column("entry_notional_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("fee_rate_bps", sa.Numeric(10, 4), nullable=True),
        sa.Column("entry_fee_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("exit_fee_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("entry_slippage_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("exit_slippage_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("entry_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("exit_spread_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mae_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("mfe_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("gross_pnl_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("net_pnl_usdt", sa.Numeric(20, 8), nullable=True),
        sa.Column("net_return_bps", sa.Numeric(12, 4), nullable=True),
        sa.Column("holding_seconds", sa.Integer(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("session_tag", sa.Text(), nullable=True),
        sa.Column("signal_snapshot", postgresql.JSONB(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["crypto_instruments.id"],
            name="fk_scalp_analytics_instrument_id_crypto_instruments",
        ),
        sa.UniqueConstraint(
            "open_client_order_id",
            name="uq_scalp_analytics_open_client_order_id",
        ),
        sa.CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="scalp_analytics_product",
        ),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="scalp_analytics_side"),
    )
    op.create_index(
        "ix_scalp_analytics_instrument_id", "scalp_trade_analytics", ["instrument_id"]
    )
    op.create_index("ix_scalp_analytics_product", "scalp_trade_analytics", ["product"])
    op.create_index(
        "ix_scalp_analytics_created_at", "scalp_trade_analytics", ["created_at"]
    )
    op.create_index(
        "ix_scalp_analytics_exit_reason", "scalp_trade_analytics", ["exit_reason"]
    )


def downgrade() -> None:
    op.drop_index("ix_scalp_analytics_exit_reason", table_name="scalp_trade_analytics")
    op.drop_index("ix_scalp_analytics_created_at", table_name="scalp_trade_analytics")
    op.drop_index("ix_scalp_analytics_product", table_name="scalp_trade_analytics")
    op.drop_index(
        "ix_scalp_analytics_instrument_id", table_name="scalp_trade_analytics"
    )
    op.drop_table("scalp_trade_analytics")
