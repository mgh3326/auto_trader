"""add alpaca_paper_order_ledger to review schema

Revision ID: c1d2e3f4a5b6
Revises: b6c7d8e9f0a1
Create Date: 2026-05-03 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Reuse existing enum — do NOT create or drop it.
instrument_type_enum = postgresql.ENUM(
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
        "alpaca_paper_order_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False, server_default="alpaca"),
        sa.Column(
            "account_mode", sa.Text(), nullable=False, server_default="alpaca_paper"
        ),
        sa.Column("lifecycle_state", sa.Text(), nullable=False),
        sa.Column("signal_symbol", sa.Text(), nullable=True),
        sa.Column("signal_venue", sa.Text(), nullable=True),
        sa.Column("execution_symbol", sa.Text(), nullable=False),
        sa.Column("execution_venue", sa.Text(), nullable=False),
        sa.Column("execution_asset_class", sa.Text(), nullable=True),
        sa.Column(
            "instrument_type",
            instrument_type_enum,
            nullable=False,
        ),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False, server_default="limit"),
        sa.Column("time_in_force", sa.Text(), nullable=True),
        sa.Column("requested_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("requested_notional", sa.Numeric(20, 4), nullable=True),
        sa.Column("requested_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False, server_default="USD"),
        sa.Column(
            "preview_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "validation_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("order_status", sa.Text(), nullable=True),
        sa.Column("filled_qty", sa.Numeric(20, 8), nullable=True),
        sa.Column("filled_avg_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("cancel_status", sa.Text(), nullable=True),
        sa.Column("canceled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "position_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("reconcile_status", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "briefing_artifact_run_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("briefing_artifact_status", sa.Text(), nullable=True),
        sa.Column("qa_evaluator_status", sa.Text(), nullable=True),
        sa.Column(
            "approval_bridge_generated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("approval_bridge_status", sa.Text(), nullable=True),
        sa.Column(
            "candidate_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("workflow_stage", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column(
            "raw_responses",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "client_order_id", name="uq_alpaca_paper_ledger_client_order_id"
        ),
        sa.CheckConstraint("broker = 'alpaca'", name="alpaca_paper_ledger_broker"),
        sa.CheckConstraint(
            "account_mode = 'alpaca_paper'",
            name="alpaca_paper_ledger_account_mode",
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ("
            "'previewed','validation_failed','submitted','open',"
            "'partially_filled','filled','canceled','unexpected'"
            ")",
            name="alpaca_paper_ledger_lifecycle_state",
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="alpaca_paper_ledger_side"),
        sa.CheckConstraint(
            "order_type IN ('limit','market')",
            name="alpaca_paper_ledger_order_type",
        ),
        sa.CheckConstraint(
            "currency IN ('USD','KRW')", name="alpaca_paper_ledger_currency"
        ),
        schema="review",
    )

    op.create_index(
        "ix_alpaca_paper_ledger_broker_order_id",
        "alpaca_paper_order_ledger",
        ["broker_order_id"],
        schema="review",
    )
    op.create_index(
        "ix_alpaca_paper_ledger_lifecycle_state",
        "alpaca_paper_order_ledger",
        ["lifecycle_state"],
        schema="review",
    )
    op.create_index(
        "ix_alpaca_paper_ledger_created_at",
        "alpaca_paper_order_ledger",
        ["created_at"],
        schema="review",
    )
    op.create_index(
        "ix_alpaca_paper_ledger_candidate_uuid",
        "alpaca_paper_order_ledger",
        ["candidate_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_alpaca_paper_ledger_briefing_run_uuid",
        "alpaca_paper_order_ledger",
        ["briefing_artifact_run_uuid"],
        schema="review",
    )
    op.create_index(
        "ix_alpaca_paper_ledger_execution_symbol",
        "alpaca_paper_order_ledger",
        ["execution_symbol"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alpaca_paper_ledger_execution_symbol",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_briefing_run_uuid",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_candidate_uuid",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_created_at",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_lifecycle_state",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_index(
        "ix_alpaca_paper_ledger_broker_order_id",
        table_name="alpaca_paper_order_ledger",
        schema="review",
    )
    op.drop_table("alpaca_paper_order_ledger", schema="review")
