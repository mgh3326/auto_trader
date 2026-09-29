"""Add H5-only durable signal, send-intent and lane-risk state.

Revision ID: 20260928_task847_h5_state
Revises: 20260929_925_krx_after_market
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260928_task847_h5_state"
down_revision: str | Sequence[str] | None = "20260929_925_krx_after_market"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "binance_h5_signals",
        sa.Column("signal_key", sa.Text(), primary_key=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("decision_ts", sa.BigInteger(), nullable=False),
        sa.Column("signal_price_text", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("entry_client_order_id", sa.Text()),
        sa.Column("entry_nav_usdt", sa.Numeric(28, 12)),
        sa.Column("entry_qty", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column("entry_price", sa.Numeric(28, 12)),
        sa.Column("entered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_qty", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column(
            "realized_pnl_usdt", sa.Numeric(28, 12), nullable=False, server_default="0"
        ),
        sa.Column("fees_usdt", sa.Numeric(28, 12), nullable=False, server_default="0"),
        sa.Column("exit_reason", sa.Text()),
        sa.Column("exit_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("exit_bar_close_ts", sa.BigInteger()),
        sa.Column("forecast_id", sa.Text()),
        sa.Column("forecast_resolved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("correlation_id", name="uq_binance_h5_signal_correlation"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_h5_signal_side"),
        sa.CheckConstraint(
            "state IN ('observed','entry_reserved','holding','closed','blocked','uncertain')",
            name="ck_binance_h5_signal_state",
        ),
        schema="review",
    )
    op.create_index(
        "ix_binance_h5_signals_symbol_state",
        "binance_h5_signals",
        ["symbol", "state"],
        schema="review",
    )
    op.create_table(
        "binance_h5_intents",
        sa.Column("client_order_id", sa.Text(), primary_key=True),
        sa.Column(
            "signal_key",
            sa.Text(),
            sa.ForeignKey("review.binance_h5_signals.signal_key"),
            nullable=False,
        ),
        sa.Column("leg_key", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(28, 12), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text()),
        sa.Column("broker_status", sa.Text()),
        sa.Column(
            "executed_qty", sa.Numeric(28, 12), nullable=False, server_default="0"
        ),
        sa.Column("avg_price", sa.Numeric(28, 12)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("signal_key", "leg_key", name="uq_binance_h5_intent_leg"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_binance_h5_intent_side"),
        sa.CheckConstraint(
            "state IN ('reserved','sending','acknowledged','evidenced','settled','uncertain')",
            name="ck_binance_h5_intent_state",
        ),
        sa.CheckConstraint("qty > 0", name="ck_binance_h5_intent_qty_positive"),
        sa.CheckConstraint(
            "executed_qty >= 0", name="ck_binance_h5_intent_executed_nonnegative"
        ),
        schema="review",
    )
    op.create_index(
        "ix_binance_h5_intents_signal",
        "binance_h5_intents",
        ["signal_key"],
        schema="review",
    )
    op.create_table(
        "binance_h5_lane_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day_kst", sa.Date(), nullable=False),
        sa.Column("day_start_nav_usdt", sa.Numeric(28, 12), nullable=False),
        sa.Column("peak_nav_usdt", sa.Numeric(28, 12), nullable=False),
        sa.Column("last_nav_usdt", sa.Numeric(28, 12), nullable=False),
        sa.Column(
            "day_entry_halted", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("halt_reason", sa.Text()),
        sa.Column("last_decision_ts", sa.BigInteger()),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="ck_binance_h5_lane_singleton"),
        schema="review",
    )
    op.create_table(
        "binance_h5_opportunities",
        sa.Column("symbol", sa.Text(), primary_key=True),
        sa.Column("decision_ts", sa.BigInteger(), primary_key=True),
        sa.Column("bar_open", sa.Numeric(28, 12), nullable=False),
        sa.Column("bar_high", sa.Numeric(28, 12), nullable=False),
        sa.Column("bar_low", sa.Numeric(28, 12), nullable=False),
        sa.Column("bar_close_text", sa.Text(), nullable=False),
        sa.Column("bid", sa.Numeric(28, 12), nullable=False),
        sa.Column("ask", sa.Numeric(28, 12), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="review",
    )
    op.create_table(
        "binance_h5_nav_samples",
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), primary_key=True),
        sa.Column("nav_usdt", sa.Numeric(28, 12), nullable=False),
        schema="review",
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN "
        "GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON review.binance_h5_signals, "
        "review.binance_h5_intents, review.binance_h5_lane_state, "
        "review.binance_h5_opportunities, review.binance_h5_nav_samples TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_table("binance_h5_nav_samples", schema="review")
    op.drop_table("binance_h5_opportunities", schema="review")
    op.drop_table("binance_h5_lane_state", schema="review")
    op.drop_index(
        "ix_binance_h5_intents_signal", table_name="binance_h5_intents", schema="review"
    )
    op.drop_table("binance_h5_intents", schema="review")
    op.drop_index(
        "ix_binance_h5_signals_symbol_state",
        table_name="binance_h5_signals",
        schema="review",
    )
    op.drop_table("binance_h5_signals", schema="review")
