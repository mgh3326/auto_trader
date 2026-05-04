"""add watch_order_intent_ledger to review schema

Revision ID: daf4130b13ce
Revises: d4e5f6a7b8c9
Create Date: 2026-05-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "daf4130b13ce"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watch_order_intent_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("condition_type", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(18, 8), nullable=False),
        sa.Column("threshold_key", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("execution_source", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=True),
        sa.Column("limit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("notional", sa.Numeric(18, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("notional_krw_input", sa.Numeric(18, 2), nullable=True),
        sa.Column("max_notional_krw", sa.Numeric(18, 2), nullable=True),
        sa.Column("notional_krw_evaluated", sa.Numeric(18, 2), nullable=True),
        sa.Column("fx_usd_krw_used", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "execution_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "blocking_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("blocked_by", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "preview_line",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("triggered_value", sa.Numeric(18, 8), nullable=True),
        sa.Column("kst_date", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("correlation_id", name="uq_watch_intent_correlation_id"),
        sa.CheckConstraint(
            "lifecycle_state IN ('previewed','failed')",
            name="watch_intent_ledger_lifecycle_state",
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="watch_intent_ledger_side"),
        sa.CheckConstraint(
            "account_mode = 'kis_mock'", name="watch_intent_ledger_account_mode"
        ),
        sa.CheckConstraint(
            "execution_source = 'watch'", name="watch_intent_ledger_execution_source"
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency IN ('KRW','USD')",
            name="watch_intent_ledger_currency",
        ),
        schema="review",
    )

    op.create_index(
        "ix_watch_intent_kst_date",
        "watch_order_intent_ledger",
        ["kst_date"],
        schema="review",
    )
    op.create_index(
        "ix_watch_intent_market_symbol",
        "watch_order_intent_ledger",
        ["market", "symbol"],
        schema="review",
    )
    op.create_index(
        "ix_watch_intent_state_created_at",
        "watch_order_intent_ledger",
        ["lifecycle_state", "created_at"],
        schema="review",
    )
    op.create_index(
        "uq_watch_intent_previewed_idempotency",
        "watch_order_intent_ledger",
        ["idempotency_key"],
        unique=True,
        schema="review",
        postgresql_where=text("lifecycle_state = 'previewed'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_watch_intent_previewed_idempotency",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_state_created_at",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_market_symbol",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_watch_intent_kst_date",
        table_name="watch_order_intent_ledger",
        schema="review",
    )
    op.drop_table("watch_order_intent_ledger", schema="review")
