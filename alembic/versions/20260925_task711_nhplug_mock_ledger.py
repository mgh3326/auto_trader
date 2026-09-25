"""Add review.nhplug_mock_order_ledger (NHPLUG Stage 2 mock order ledger).

Revision ID: 20260925_task711_nhplug_ledger
Revises: 20260925_task691_toss_expired
Create Date: 2026-09-25

Additive DDL only: one new table plus two indexes. No existing table, column,
constraint, or row is touched. Operators apply it separately with
``alembic upgrade head``; nothing auto-applies it. The table CHECKs pin the
Stage 2 scope (broker='nhplug', account_mode='nhplug_mock', venue='KRX',
order_type='limit') and store no account number. Constraint names are pinned
with ``op.f`` to the ORM naming-convention spelling so migrated and
``create_all`` schemas converge on one name.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260925_task711_nhplug_ledger"
down_revision: str | Sequence[str] | None = "20260925_task691_toss_expired"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "nhplug_mock_order_ledger"
_SCHEMA = "review"
_STATUSES = (
    "submitting",
    "not_submitted",
    "accepted",
    "acceptance_uncertain",
    "rejected",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "modified",
    "confirmed",
    "anomaly",
)
_RECONCILE_STATES = ("pending", "verified", "unknown", "source_disagreement")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{value}'" for value in values) + ")"


def _ck(name: str) -> str:
    return op.f(f"ck_{_TABLE}_{name}")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("client_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_date", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("operation_kind", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(20, 0), nullable=True),
        sa.Column("price", sa.Numeric(20, 3), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("original_order_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "reconcile_state",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("response_code", sa.Text(), nullable=True),
        sa.Column("response_message", sa.Text(), nullable=True),
        sa.Column("filled_qty", sa.Numeric(20, 0), nullable=True),
        sa.Column("avg_fill_price", sa.Numeric(20, 3), nullable=True),
        sa.Column("open_qty", sa.Numeric(20, 0), nullable=True),
        sa.Column("cancelled_qty", sa.Numeric(20, 0), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "last_reconcile", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "requires_manual_review",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("manual_review_reason", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint("client_request_id", name="uq_nhplug_mock_ledger_request"),
        sa.UniqueConstraint(
            "order_date",
            "broker_order_id",
            name="uq_nhplug_mock_ledger_date_broker_order",
        ),
        sa.CheckConstraint("broker = 'nhplug'", name=_ck("broker_nhplug")),
        sa.CheckConstraint(
            "account_mode = 'nhplug_mock'", name=_ck("account_mode_mock")
        ),
        sa.CheckConstraint("venue = 'KRX'", name=_ck("venue_krx")),
        sa.CheckConstraint("order_type = 'limit'", name=_ck("order_type_limit")),
        sa.CheckConstraint(
            "operation_kind IN ('place','modify','cancel')",
            name=_ck("operation_kind"),
        ),
        sa.CheckConstraint("side IS NULL OR side IN ('buy','sell')", name=_ck("side")),
        sa.CheckConstraint("symbol ~ '^[0-9]{6}$'", name=_ck("symbol_krx")),
        sa.CheckConstraint("order_date ~ '^[0-9]{8}$'", name=_ck("order_date_format")),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0", name=_ck("quantity_positive")
        ),
        sa.CheckConstraint("price IS NULL OR price > 0", name=_ck("price_positive")),
        sa.CheckConstraint(
            "operation_kind = 'cancel' OR (quantity IS NOT NULL AND price IS NOT NULL)",
            name=_ck("place_modify_need_limit"),
        ),
        sa.CheckConstraint(
            "operation_kind = 'place' OR original_order_id IS NOT NULL",
            name=_ck("modify_cancel_need_original"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('accepted','open','partially_filled','filled',"
            "'cancelled','modified','confirmed') OR broker_order_id IS NOT NULL",
            name=_ck("accepted_needs_broker_order_id"),
        ),
        sa.CheckConstraint(_in_list("status", _STATUSES), name=_ck("status")),
        sa.CheckConstraint(
            _in_list("reconcile_state", _RECONCILE_STATES),
            name=_ck("reconcile_state"),
        ),
        sa.CheckConstraint(
            "filled_qty IS NULL OR filled_qty >= 0",
            name=_ck("filled_qty_nonnegative"),
        ),
        sa.CheckConstraint(
            "filled_qty IS NULL OR quantity IS NULL OR filled_qty <= quantity",
            name=_ck("filled_within_quantity"),
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_nhplug_mock_ledger_order_date_status",
        _TABLE,
        ["order_date", "status"],
        schema=_SCHEMA,
    )
    op.create_index("ix_nhplug_mock_ledger_symbol", _TABLE, ["symbol"], schema=_SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_nhplug_mock_ledger_symbol", table_name=_TABLE, schema=_SCHEMA)
    op.drop_index(
        "ix_nhplug_mock_ledger_order_date_status", table_name=_TABLE, schema=_SCHEMA
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
