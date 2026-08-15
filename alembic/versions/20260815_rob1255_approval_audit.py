"""Add append-only approval audit facts without changing approval decisions.

Revision ID: 20260815_rob1255_audit
Revises: 20260814_lcapprove_b1
Create Date: 2026-08-15

The new table is additive and intentionally has no backfill. Existing latest-
dispatch columns and the dispatch-attempt ledger remain unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260815_rob1255_audit"
down_revision: str = "20260814_lcapprove_b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"


def upgrade() -> None:
    op.create_table(
        "order_proposal_approval_audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_pk", sa.BigInteger(), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "rung_indices",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_result", sa.Text()),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("timing_source", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text()),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("nonce_digest", sa.Text()),
        sa.Column(
            "nonce_consumed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "nonce_invalidated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("dispatch_attempt_id", postgresql.UUID(as_uuid=True)),
        sa.Column("card_chat_id", sa.Text()),
        sa.Column("card_message_id", sa.BigInteger()),
        sa.Column("card_kind", sa.Text()),
        sa.Column("predecessor_proposal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("successor_proposal_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reason_code", sa.Text()),
        sa.Column("details", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'expired','first_stage_approved','second_stage_clicked',"
            "'second_stage_dispatched','superseded')",
            name="order_proposal_approval_audit_events_type",
        ),
        sa.CheckConstraint(
            "timing_source IN ("
            "'approval_deadline','proposal_deadline','supersede_transaction',"
            "'telegram_callback_received','telegram_dispatch_started')",
            name="order_proposal_approval_audit_events_timing_source",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('system','telegram_user','web_user')",
            name="order_proposal_approval_audit_events_actor_kind",
        ),
        sa.CheckConstraint(
            "channel IN ('system','telegram','web')",
            name="order_proposal_approval_audit_events_channel",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rung_indices) = 'array'",
            name="order_proposal_approval_audit_events_rung_indices",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_pk"],
            ["review.order_proposals.id"],
            name="fk_order_proposal_approval_audit_event_proposal",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "event_id", name="uq_order_proposal_approval_audit_events_event_id"
        ),
        schema=_SCHEMA,
        comment=(
            "Append-only forensic facts. This table is never an approval gate or "
            "authorization source."
        ),
    )
    op.create_index(
        "ix_order_proposal_approval_audit_events_proposal",
        "order_proposal_approval_audit_events",
        ["proposal_pk", "occurred_at"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_order_proposal_approval_audit_events_root",
        "order_proposal_approval_audit_events",
        ["root_proposal_id", "occurred_at"],
        schema=_SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION review.reject_order_proposal_approval_audit_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'order proposal approval audit events are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_proposal_approval_audit_events_append_only
        BEFORE UPDATE OR DELETE ON review.order_proposal_approval_audit_events
        FOR EACH ROW EXECUTE FUNCTION
            review.reject_order_proposal_approval_audit_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_proposal_approval_audit_events_truncate_append_only
        BEFORE TRUNCATE ON review.order_proposal_approval_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION
            review.reject_order_proposal_approval_audit_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_order_proposal_approval_audit_events_truncate_append_only "
        "ON review.order_proposal_approval_audit_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_order_proposal_approval_audit_events_append_only "
        "ON review.order_proposal_approval_audit_events"
    )
    op.drop_index(
        "ix_order_proposal_approval_audit_events_root",
        table_name="order_proposal_approval_audit_events",
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_order_proposal_approval_audit_events_proposal",
        table_name="order_proposal_approval_audit_events",
        schema=_SCHEMA,
    )
    op.drop_table("order_proposal_approval_audit_events", schema=_SCHEMA)
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.reject_order_proposal_approval_audit_event_mutation()"
    )
