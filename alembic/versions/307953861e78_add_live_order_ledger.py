"""add live_order_ledger

Revision ID: 307953861e78
Revises: 14fa36b85d0a
Create Date: 2026-06-01 17:33:33.646737

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '307953861e78'
down_revision: Union[str, Sequence[str], None] = 'rob337_add_watch_recommendation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "live_order_ledger",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text(), nullable=True),
        sa.Column("market_symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_kind", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("amount", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("order_no", sa.Text(), nullable=True),
        sa.Column("order_time", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("target_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("min_hold_days", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("exit_reason", sa.Text(), nullable=True),
        sa.Column("indicators_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("dt_approval_issue_id", sa.Text(), nullable=True),
        sa.Column("dt_requester_agent_id", sa.Text(), nullable=True),
        sa.Column("dt_caller_source", sa.Text(), nullable=True),
        sa.Column("filled_qty", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("journal_id", sa.BigInteger(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_order_ledger")),
        sa.UniqueConstraint("broker", "account_scope", "order_no", name="uq_live_ledger_order"),
        schema="review",
    )
    op.create_index("ix_live_ledger_status", "live_order_ledger", ["status"], unique=False, schema="review")
    op.create_index("ix_live_ledger_market_symbol", "live_order_ledger", ["market", "symbol"], unique=False, schema="review")


def downgrade() -> None:
    op.drop_index("ix_live_ledger_market_symbol", table_name="live_order_ledger", schema="review")
    op.drop_index("ix_live_ledger_status", table_name="live_order_ledger", schema="review")
    op.drop_table("live_order_ledger", schema="review")
