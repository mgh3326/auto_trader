"""Add channel-neutral loss-cut approval ceremony records.

Revision ID: 20260814_lcapprove_b1
Revises: 20260805_toss_merge
Create Date: 2026-08-14

This revision is additive.  It does not backfill approvals or create an order
submission path; existing dispatch attempts are classified as Telegram rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260814_lcapprove_b1"
down_revision: str = "20260805_toss_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"


def upgrade() -> None:
    for definition in (
        "approval_dispatch_channel TEXT",
        "approval_dispatch_scope_hash TEXT",
        "approval_dispatch_evidence_hash TEXT",
        "approved_by_channel TEXT",
        "approved_by_subject TEXT",
    ):
        op.execute(
            f"ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS {definition}"
        )
    op.create_check_constraint(
        "order_proposals_approval_dispatch_channel",
        "order_proposals",
        "approval_dispatch_channel IS NULL OR "
        "approval_dispatch_channel IN ('telegram','web')",
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "order_proposals_approved_by_channel",
        "order_proposals",
        "approved_by_channel IS NULL OR approved_by_channel IN ('telegram','web')",
        schema=_SCHEMA,
    )

    op.add_column(
        "order_proposal_approval_dispatch_attempts",
        sa.Column(
            "channel",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'telegram'"),
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "order_proposal_approval_dispatch_attempts",
        sa.Column("scope_hash", sa.Text()),
        schema=_SCHEMA,
    )
    op.add_column(
        "order_proposal_approval_dispatch_attempts",
        sa.Column("evidence_hash", sa.Text()),
        schema=_SCHEMA,
    )
    op.add_column(
        "order_proposal_approval_dispatch_attempts",
        sa.Column("publication_ref_digest", sa.Text()),
        schema=_SCHEMA,
    )
    op.create_check_constraint(
        "order_proposal_approval_dispatch_attempt_channel",
        "order_proposal_approval_dispatch_attempts",
        "channel IN ('telegram','web')",
        schema=_SCHEMA,
    )

    op.create_table(
        "order_proposal_loss_cut_scopes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("proposal_pk", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("account_ref", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("requested_quantity", sa.Numeric(38, 12), nullable=False),
        sa.Column("observed_total_quantity", sa.Numeric(38, 12), nullable=False),
        sa.Column("observed_sellable_quantity", sa.Numeric(38, 12), nullable=False),
        sa.Column("average_price", sa.Numeric(38, 12), nullable=False),
        sa.Column("position_scope", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decision_observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("evidence_valid_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("scope_hash", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.Text(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["proposal_pk"],
            ["review.order_proposals.id"],
            ondelete="CASCADE",
            name="fk_order_proposal_loss_cut_scope_proposal",
        ),
        sa.UniqueConstraint(
            "proposal_pk", name="uq_order_proposal_loss_cut_scopes_proposal"
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_order_proposal_loss_cut_scopes_scope_hash",
        "order_proposal_loss_cut_scopes",
        ["scope_hash"],
        schema=_SCHEMA,
    )

    op.create_table(
        "order_proposal_approval_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_pk", sa.BigInteger(), nullable=False),
        sa.Column("ceremony_digest", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("step", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_subject", sa.Text(), nullable=False),
        sa.Column("actor_role", sa.Text()),
        sa.Column("dispatch_attempt_id", postgresql.UUID(as_uuid=True)),
        sa.Column("membership_revision", sa.Integer()),
        sa.Column("membership_digest", sa.Text()),
        sa.Column("nonce_digest", sa.Text()),
        sa.Column("proposal_payload_hash", sa.Text()),
        sa.Column("scope_hash", sa.Text()),
        sa.Column("evidence_hash", sa.Text()),
        sa.Column("evidence_snapshot", postgresql.JSONB()),
        sa.Column("reason_code", sa.Text()),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "channel IN ('telegram','web')",
            name="order_proposal_approval_events_channel",
        ),
        sa.CheckConstraint(
            "step IN ('begin','confirm')",
            name="order_proposal_approval_events_step",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted','rejected','needs_reconfirm','expired')",
            name="order_proposal_approval_events_outcome",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('user','telegram')",
            name="order_proposal_approval_events_actor_kind",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_pk"],
            ["review.order_proposals.id"],
            ondelete="RESTRICT",
            name="fk_order_proposal_approval_event_proposal",
        ),
        sa.UniqueConstraint(
            "event_id", name="uq_order_proposal_approval_events_event_id"
        ),
        sa.UniqueConstraint(
            "proposal_pk",
            "ceremony_digest",
            "step",
            name="uq_order_proposal_approval_events_ceremony_step",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_order_proposal_approval_events_proposal_observed",
        "order_proposal_approval_events",
        ["proposal_pk", "observed_at"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_order_proposal_approval_events_ceremony",
        "order_proposal_approval_events",
        ["ceremony_digest"],
        schema=_SCHEMA,
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION review.reject_order_proposal_approval_event_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'order proposal approval events are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_proposal_approval_events_append_only
        BEFORE UPDATE OR DELETE ON review.order_proposal_approval_events
        FOR EACH ROW EXECUTE FUNCTION
            review.reject_order_proposal_approval_event_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_order_proposal_approval_events_truncate_append_only
        BEFORE TRUNCATE ON review.order_proposal_approval_events
        FOR EACH STATEMENT EXECUTE FUNCTION
            review.reject_order_proposal_approval_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_order_proposal_approval_events_truncate_append_only "
        "ON review.order_proposal_approval_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_proposal_approval_events_append_only "
        "ON review.order_proposal_approval_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS review.reject_order_proposal_approval_event_mutation()"
    )
    op.drop_table("order_proposal_approval_events", schema=_SCHEMA)
    op.drop_table("order_proposal_loss_cut_scopes", schema=_SCHEMA)
    op.drop_constraint(
        "order_proposal_approval_dispatch_attempt_channel",
        "order_proposal_approval_dispatch_attempts",
        schema=_SCHEMA,
        type_="check",
    )
    for column in (
        "publication_ref_digest",
        "evidence_hash",
        "scope_hash",
        "channel",
    ):
        op.drop_column(
            "order_proposal_approval_dispatch_attempts", column, schema=_SCHEMA
        )
    op.drop_constraint(
        "order_proposals_approved_by_channel",
        "order_proposals",
        schema=_SCHEMA,
        type_="check",
    )
    op.drop_constraint(
        "order_proposals_approval_dispatch_channel",
        "order_proposals",
        schema=_SCHEMA,
        type_="check",
    )
    for column in (
        "approved_by_subject",
        "approved_by_channel",
        "approval_dispatch_evidence_hash",
        "approval_dispatch_scope_hash",
        "approval_dispatch_channel",
    ):
        op.drop_column("order_proposals", column, schema=_SCHEMA)
