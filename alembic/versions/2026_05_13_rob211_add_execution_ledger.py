"""ROB-211 add execution ledger tables.

Revision ID: 20260513_rob211
Revises: 1a2b3c4d5e6f
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260513_rob211"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None

NOW_SQL = sa.text("now()")
FILLED_AT_DESC = sa.text("filled_at DESC")
STARTED_AT_DESC = sa.text("started_at DESC")

instrument_type = postgresql.ENUM(
    "equity_kr",
    "equity_us",
    "crypto",
    "forex",
    "index",
    name="instrument_type",
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "execution_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False, server_default="live"),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("instrument_type", instrument_type, nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("raw_symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=False),
        sa.Column("fill_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=False),
        sa.Column("filled_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("filled_notional", sa.Numeric(20, 4), nullable=False),
        sa.Column("fee_amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("fee_currency", sa.Text(), nullable=True),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="reconciler"),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=NOW_SQL,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=NOW_SQL,
        ),
        sa.CheckConstraint("broker IN ('kis','upbit')", name="execution_ledger_broker"),
        sa.CheckConstraint(
            "account_mode IN ('live','mock')", name="execution_ledger_account_mode"
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="execution_ledger_side"),
        sa.CheckConstraint(
            "currency IN ('KRW','USD')", name="execution_ledger_currency"
        ),
        sa.CheckConstraint(
            "source IN ('reconciler','websocket','manual_import')",
            name="execution_ledger_source",
        ),
        sa.CheckConstraint(
            "fill_seq >= 0", name="execution_ledger_fill_seq_nonnegative"
        ),
        sa.CheckConstraint(
            "filled_qty > 0", name="execution_ledger_filled_qty_positive"
        ),
        sa.CheckConstraint(
            "filled_price > 0", name="execution_ledger_filled_price_positive"
        ),
        sa.UniqueConstraint(
            "broker",
            "broker_order_id",
            "fill_seq",
            name="uq_execution_ledger_broker_order_fill",
        ),
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_filled_at",
        "execution_ledger",
        [FILLED_AT_DESC],
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_symbol_filled_at",
        "execution_ledger",
        ["symbol", FILLED_AT_DESC],
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_broker_filled_at",
        "execution_ledger",
        ["broker", FILLED_AT_DESC],
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_source_run_id",
        "execution_ledger",
        ["source_run_id"],
        schema="review",
    )

    op.create_table(
        "execution_ledger_reconcile_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=NOW_SQL,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("would_insert", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("would_update", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_insert", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_update", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "broker IN ('kis','upbit')", name="execution_ledger_runs_broker"
        ),
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_runs_broker_window",
        "execution_ledger_reconcile_runs",
        ["broker", "window_start"],
        schema="review",
    )
    op.create_index(
        "ix_execution_ledger_runs_started_at",
        "execution_ledger_reconcile_runs",
        [STARTED_AT_DESC],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_ledger_runs_started_at",
        table_name="execution_ledger_reconcile_runs",
        schema="review",
    )
    op.drop_index(
        "ix_execution_ledger_runs_broker_window",
        table_name="execution_ledger_reconcile_runs",
        schema="review",
    )
    op.drop_table("execution_ledger_reconcile_runs", schema="review")
    op.drop_index(
        "ix_execution_ledger_source_run_id",
        table_name="execution_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_execution_ledger_broker_filled_at",
        table_name="execution_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_execution_ledger_symbol_filled_at",
        table_name="execution_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_execution_ledger_filled_at", table_name="execution_ledger", schema="review"
    )
    op.drop_table("execution_ledger", schema="review")
