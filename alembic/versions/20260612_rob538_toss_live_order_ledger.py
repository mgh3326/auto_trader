"""ROB-538 add Toss live order ledger.

Revision ID: 20260612_rob538_toss_live_order_ledger
Revises: 20260611_rob516_rob512_merge
Create Date: 2026-06-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260612_rob538_toss_live_order_ledger"
down_revision: Union[str, Sequence[str], None] = "20260611_rob516_rob512_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "toss_live_order_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("time_in_force", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("price", sa.Numeric(20, 8), nullable=True),
        sa.Column("order_amount", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("original_order_id", sa.Text(), nullable=True),
        sa.Column("replaced_by_order_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("broker_status", sa.Text(), nullable=True),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_item_uuid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("commission", sa.Numeric(20, 8), nullable=True),
        sa.Column("tax", sa.Numeric(20, 8), nullable=True),
        sa.Column("settlement_date", sa.Date(), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("broker = 'toss'", name="toss_live_ledger_broker_toss"),
        sa.CheckConstraint("account_mode = 'toss_live'", name="toss_live_ledger_account_mode_toss_live"),
        sa.CheckConstraint("operation_kind IN ('place','modify','cancel')", name="toss_live_ledger_operation_kind"),
        sa.CheckConstraint("market IN ('kr','us')", name="toss_live_ledger_market"),
        sa.CheckConstraint("side IN ('buy','sell')", name="toss_live_ledger_side"),
        sa.CheckConstraint("order_type IN ('limit','market')", name="toss_live_ledger_order_type"),
        sa.CheckConstraint(
            "status IN ('accepted','rejected','pending','partial','filled','cancelled','replaced','cancel_rejected','replace_rejected','anomaly')",
            name="toss_live_ledger_status",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_toss_live_order_ledger")),
        sa.UniqueConstraint("client_order_id", name="uq_toss_live_ledger_client_order_id"),
        sa.UniqueConstraint("broker_order_id", name="uq_toss_live_ledger_broker_order_id"),
        schema="review",
    )
    op.create_index("ix_toss_live_ledger_status", "toss_live_order_ledger", ["status"], schema="review")
    op.create_index("ix_toss_live_ledger_market_symbol", "toss_live_order_ledger", ["market", "symbol"], schema="review")
    op.create_index("ix_toss_live_ledger_broker_status", "toss_live_order_ledger", ["broker_status"], schema="review")
    op.create_index("ix_toss_live_ledger_report_item_uuid", "toss_live_order_ledger", ["report_item_uuid"], schema="review")
    op.create_index("ix_toss_live_ledger_replaced_by", "toss_live_order_ledger", ["replaced_by_order_id"], schema="review")


def downgrade() -> None:
    op.drop_index("ix_toss_live_ledger_replaced_by", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_report_item_uuid", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_broker_status", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_market_symbol", table_name="toss_live_order_ledger", schema="review")
    op.drop_index("ix_toss_live_ledger_status", table_name="toss_live_order_ledger", schema="review")
    op.drop_table("toss_live_order_ledger", schema="review")
