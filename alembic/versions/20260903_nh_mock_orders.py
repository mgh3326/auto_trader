"""add NH mock mirror signal and accepted-send ledgers

Revision ID: 20260903_nh_mock_orders
Revises: 20260902_rob1340_authority
Create Date: 2026-09-03 10:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_nh_mock_orders"
down_revision: str | Sequence[str] | None = "20260902_rob1340_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names(schema="review")


def upgrade() -> None:
    # CI sometimes creates ORM metadata first.  These guards make the migration
    # additive in both that test setup and a normal empty review schema.
    if not _has_table("nh_mock_signal_ledger"):
        op.create_table(
            "nh_mock_signal_ledger",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.Column("strategy", sa.Text(), nullable=False),
            sa.Column("signal_source", sa.Text(), nullable=False),
            sa.Column(
                "account_mode", sa.Text(), nullable=False, server_default="nh_mock"
            ),
            sa.Column("symbol", sa.Text(), nullable=False),
            sa.Column("side", sa.Text(), nullable=False),
            sa.Column("intended_quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("intended_price", sa.Numeric(20, 4), nullable=False),
            sa.Column("counterfactual_of", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "correlation_id", name="uq_nh_mock_signal_correlation_id"
            ),
            sa.CheckConstraint(
                "account_mode = 'nh_mock'", name="ck_nh_mock_signal_account_mode"
            ),
            sa.CheckConstraint(
                "length(btrim(correlation_id)) > 0",
                name="ck_nh_mock_signal_correlation_nonblank",
            ),
            sa.CheckConstraint(
                "length(btrim(strategy)) > 0",
                name="ck_nh_mock_signal_strategy_nonblank",
            ),
            sa.CheckConstraint(
                "length(btrim(signal_source)) > 0",
                name="ck_nh_mock_signal_source_nonblank",
            ),
            schema="review",
        )
    if not _has_table("nh_mock_order_ledger"):
        op.create_table(
            "nh_mock_order_ledger",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("client_order_id", sa.Text(), nullable=False),
            sa.Column("broker_order_id", sa.Text()),
            sa.Column(
                "account_mode", sa.Text(), nullable=False, server_default="nh_mock"
            ),
            sa.Column("correlation_id", sa.Text(), nullable=False),
            sa.Column("counterfactual_of", sa.UUID()),
            sa.Column("strategy", sa.Text(), nullable=False),
            sa.Column("symbol", sa.Text(), nullable=False),
            sa.Column("side", sa.Text(), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
            sa.Column("price", sa.Numeric(20, 4), nullable=False),
            sa.Column("status", sa.Text(), nullable=False, server_default="accepted"),
            sa.Column("response_code", sa.Text()),
            sa.Column("raw_response", sa.dialects.postgresql.JSONB()),
            sa.Column(
                "filled_quantity", sa.Numeric(20, 8), nullable=False, server_default="0"
            ),
            sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True)),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "broker_order_id", name="uq_nh_mock_ledger_broker_order_id"
            ),
            sa.UniqueConstraint(
                "client_order_id", name="uq_nh_mock_ledger_client_order_id"
            ),
            sa.CheckConstraint(
                "account_mode = 'nh_mock'", name="ck_nh_mock_ledger_account_mode"
            ),
            sa.CheckConstraint(
                "side IN ('buy','sell','cancel')", name="ck_nh_mock_ledger_side"
            ),
            sa.CheckConstraint(
                "status IN ('accepted','pending','partial','filled','cancelled','anomaly')",
                name="ck_nh_mock_ledger_status",
            ),
            schema="review",
        )
        op.create_index(
            "ix_nh_mock_ledger_correlation_id",
            "nh_mock_order_ledger",
            ["correlation_id"],
            schema="review",
        )
        op.create_index(
            "ix_nh_mock_ledger_status",
            "nh_mock_order_ledger",
            ["status"],
            schema="review",
        )


def downgrade() -> None:
    if _has_table("nh_mock_order_ledger"):
        op.drop_table("nh_mock_order_ledger", schema="review")
    if _has_table("nh_mock_signal_ledger"):
        op.drop_table("nh_mock_signal_ledger", schema="review")
