"""Add Phase 0 UUID context outcomes without economic intent.

Revision ID: 20260908_task137_ctx_outcomes
Revises: 20260907_rob1351_lifecycle
Create Date: 2026-09-08

This migration adds one table owned by the scheduleless fill/watch context
consumer. It does not alter an execution ledger, account table, proposal,
watch, token, capability, scheduler, or any existing safety gate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260908_task137_ctx_outcomes"
down_revision: str | Sequence[str] | None = "20260907_rob1351_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fill_watch_context_outcomes"
_SCHEMA = "review"
_STATUSES = (
    "'context_only_no_action', 'stale_input', 'failed_processing', 'needs_human'"
)
_REASONS = (
    "'context_recorded', 'close_condition_recorded', 'stale_market_snapshot', "
    "'stale_position_snapshot', 'artifact_declared_failed', 'route_unavailable', "
    "'event_uuid_conflict'"
)
_NEXT_ACTIONS = (
    "'none', 'refresh_context', 'inspect_artifact', 'await_route', 'operator_review'"
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "transport_event_uuid", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("transport_event_id", sa.Text(), nullable=False),
        sa.Column("responsible_consumer", sa.Text(), nullable=False),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("input_as_of", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("economic_root_ref", sa.Text(), nullable=False),
        sa.Column(
            "order_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("contextual_status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("next_action", sa.Text(), nullable=False),
        sa.Column("semantic_digest", sa.Text(), nullable=False),
        sa.Column(
            "delivery_count", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "conflict_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
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
        sa.CheckConstraint(
            "transport_event_id = transport_event_uuid::text",
            name="canonical_transport_event_id",
        ),
        sa.CheckConstraint(
            "responsible_consumer = 'fill_watch_context'",
            name="responsible_consumer",
        ),
        sa.CheckConstraint("event_kind IN ('fill', 'watch')", name="event_kind"),
        sa.CheckConstraint(
            f"contextual_status IN ({_STATUSES})",
            name="contextual_status",
        ),
        sa.CheckConstraint(f"reason IN ({_REASONS})", name="reason"),
        sa.CheckConstraint(f"next_action IN ({_NEXT_ACTIONS})", name="next_action"),
        sa.CheckConstraint(
            "length(btrim(economic_root_ref)) > 0",
            name="economic_root_ref",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(order_refs) = 'array' AND jsonb_array_length(order_refs) >= 1",
            name="order_refs",
        ),
        sa.CheckConstraint("delivery_count >= 1", name="delivery_count"),
        sa.CheckConstraint("conflict_count >= 0", name="conflict_count"),
        sa.CheckConstraint(
            "semantic_digest ~ '^[0-9a-f]{64}$'",
            name="semantic_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "transport_event_uuid",
            name="uq_fill_watch_context_outcomes_transport_event_uuid",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_watch_context_outcomes_economic_root_ref",
        _TABLE,
        ["economic_root_ref"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_fill_watch_context_outcomes_contextual_status",
        _TABLE,
        ["contextual_status"],
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table(_TABLE, schema=_SCHEMA)
