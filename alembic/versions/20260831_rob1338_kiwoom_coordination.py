"""Add the durable KR Kiwoom coordination lifecycle store.

Revision ID: 20260831_rob1338_kiwoom_coord
Revises: 20260830_rob1331_q6_epoch
Create Date: 2026-08-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260831_rob1338_kiwoom_coord"
down_revision: str | Sequence[str] | None = "20260830_rob1331_q6_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPATCH_ALL_OR_NONE_SQL = (
    "CASE WHEN dispatch_kind IS NULL THEN "
    "dispatch_envelope IS NULL AND mutation_certainty IS NULL "
    "AND claim_row_id IS NULL AND callback_failed IS NULL "
    "AND ack_attachment_failed IS NULL "
    "AND outer_cancellation_requested IS NULL "
    "ELSE dispatch_envelope IS NOT NULL "
    "AND mutation_certainty IS NOT NULL AND claim_row_id IS NOT NULL "
    "AND callback_failed IS NOT NULL AND ack_attachment_failed IS NOT NULL "
    "AND outer_cancellation_requested IS NOT NULL END"
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "kiwoom_coordination_lifecycle",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lane_id", sa.Text(), nullable=False),
        sa.Column("physical_account_id", sa.Text(), nullable=False),
        sa.Column("claim_account_scope", sa.Text(), nullable=False),
        sa.Column("decision_intent_id", sa.Text(), nullable=False),
        sa.Column("execution_plan_id", sa.Text(), nullable=False),
        sa.Column("order_attempt_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "initial_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "ack_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "dispatch_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("dispatch_kind", sa.Text(), nullable=True),
        sa.Column("mutation_certainty", sa.Text(), nullable=True),
        sa.Column("claim_row_id", sa.BigInteger(), nullable=True),
        sa.Column("callback_failed", sa.Boolean(), nullable=True),
        sa.Column("ack_attachment_failed", sa.Boolean(), nullable=True),
        sa.Column("outer_cancellation_requested", sa.Boolean(), nullable=True),
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
        sa.CheckConstraint(
            "lane_id = 'kr.kiwoom.mock'",
            name="lane_kr_kiwoom_mock",
        ),
        sa.CheckConstraint(
            "dispatch_kind IS NULL OR dispatch_kind IN ("
            "'acknowledged','definitive_without_broker_id',"
            "'lane_reported_uncertain','callback_failed','ack_attachment_failed'"
            ")",
            name="dispatch_kind",
        ),
        sa.CheckConstraint(
            "mutation_certainty IS NULL OR mutation_certainty IN ("
            "'definitive','uncertain')",
            name="mutation_certainty",
        ),
        sa.CheckConstraint(
            _DISPATCH_ALL_OR_NONE_SQL,
            name="dispatch_all_or_none",
        ),
        sa.CheckConstraint(
            "ack_envelope IS NULL OR broker_order_id IS NOT NULL",
            name="ack_envelope_has_broker_order_id",
        ),
        sa.CheckConstraint(
            "CASE WHEN dispatch_kind = 'acknowledged' THEN "
            "broker_order_id IS NOT NULL AND ack_envelope IS NOT NULL "
            "ELSE true END",
            name="acknowledged_has_ack",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "claim_account_scope",
            "idempotency_key",
            name="uq_kiwoom_coordination_scope_key",
        ),
        sa.UniqueConstraint(
            "order_attempt_id",
            name="uq_kiwoom_coordination_order_attempt",
        ),
        sa.UniqueConstraint(
            "claim_row_id",
            name="uq_kiwoom_coordination_claim_row",
        ),
        schema="review",
    )
    op.create_index(
        "ix_kiwoom_coordination_scope_dispatch",
        "kiwoom_coordination_lifecycle",
        ["claim_account_scope", "dispatch_kind"],
        schema="review",
    )
    op.create_index(
        "ix_kiwoom_coordination_broker_order_id",
        "kiwoom_coordination_lifecycle",
        ["broker_order_id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kiwoom_coordination_broker_order_id",
        table_name="kiwoom_coordination_lifecycle",
        schema="review",
    )
    op.drop_index(
        "ix_kiwoom_coordination_scope_dispatch",
        table_name="kiwoom_coordination_lifecycle",
        schema="review",
    )
    op.drop_table("kiwoom_coordination_lifecycle", schema="review")
