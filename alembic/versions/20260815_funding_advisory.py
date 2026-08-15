"""Add funding advisory threads, revisions, deliveries, and proposal links.

Revision ID: 20260815_funding_advisory
Revises: 20260815_external_cash
Create Date: 2026-08-15

Additive DDL only. No candidate, delivery, proposal, or cash declaration row is
created by this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260815_funding_advisory"
down_revision: str | Sequence[str] | None = "20260815_external_cash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "funding_advisories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("advisory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_key", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_candidate_id", sa.Text(), nullable=False),
        sa.Column("gate_name", sa.Text(), nullable=False),
        sa.Column("gate_version", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_account_mode", sa.Text(), nullable=False),
        sa.Column("broker_account_id", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(length=3), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), server_default=sa.text("'buy'"), nullable=False),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=False),
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
            "market IN ('crypto','equity_kr','equity_us')",
            name="ck_funding_advisory_market",
        ),
        sa.CheckConstraint("side = 'buy'", name="ck_funding_advisory_buy_only"),
        sa.CheckConstraint(
            "state IN ('active','resolved','superseded')",
            name="ck_funding_advisory_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_funding_advisory_owner_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("advisory_id", name="uq_funding_advisory_id"),
        sa.UniqueConstraint("thread_key", name="uq_funding_advisory_thread_key"),
        schema="review",
    )
    op.create_index(
        "ix_funding_advisory_owner_state",
        "funding_advisories",
        ["owner_user_id", "state"],
        schema="review",
    )
    op.create_index(
        "ix_funding_advisory_candidate",
        "funding_advisories",
        ["source_kind", "source_candidate_id"],
        schema="review",
    )

    op.create_table(
        "funding_advisory_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("advisory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("required_cash", sa.Numeric(38, 12), nullable=False),
        sa.Column("target_buying_power", sa.Numeric(38, 12), nullable=False),
        sa.Column(
            "other_pending_required",
            sa.Numeric(38, 12),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "reserved_cash",
            sa.Numeric(38, 12),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("shortfall", sa.Numeric(38, 12), nullable=False),
        sa.Column("operational_gap", sa.Numeric(38, 12), nullable=False),
        sa.Column("routes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "combination", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("evaluated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "required_cash >= 0 AND target_buying_power >= 0 "
            "AND other_pending_required >= 0 AND reserved_cash >= 0 "
            "AND shortfall >= 0 AND operational_gap >= 0",
            name="ck_funding_revision_amounts_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["advisory_id"],
            ["review.funding_advisories.advisory_id"],
            name="fk_funding_revision_advisory",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", name="uq_funding_advisory_revision_id"),
        sa.UniqueConstraint(
            "advisory_id",
            "revision_no",
            name="uq_funding_advisory_revision_no",
        ),
        sa.UniqueConstraint(
            "advisory_id",
            "fingerprint",
            name="uq_funding_advisory_fingerprint",
        ),
        schema="review",
    )
    op.create_index(
        "ix_funding_revision_advisory_evaluated",
        "funding_advisory_revisions",
        ["advisory_id", "evaluated_at"],
        schema="review",
    )

    op.create_table(
        "funding_advisory_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("advisory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            sa.Text(),
            server_default=sa.text("'telegram'"),
            nullable=False,
        ),
        sa.Column("kst_date", sa.Date(), nullable=False),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'claimed'"), nullable=False
        ),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("channel = 'telegram'", name="ck_funding_delivery_channel"),
        sa.CheckConstraint(
            "state IN ('claimed','sent','send_failed','edit_failed','delivery_unknown')",
            name="ck_funding_delivery_state",
        ),
        sa.ForeignKeyConstraint(
            ["advisory_id"],
            ["review.funding_advisories.advisory_id"],
            name="fk_funding_delivery_advisory",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["review.funding_advisory_revisions.revision_id"],
            name="fk_funding_delivery_revision",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_funding_delivery_id"),
        sa.UniqueConstraint(
            "advisory_id",
            "channel",
            "kst_date",
            name="uq_funding_delivery_advisory_channel_date",
        ),
        schema="review",
    )
    op.create_index(
        "ix_funding_delivery_state",
        "funding_advisory_deliveries",
        ["state", "kst_date"],
        schema="review",
    )

    op.create_table(
        "funding_advisory_proposal_links",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("advisory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_kind",
            sa.Text(),
            server_default=sa.text("'order_proposal_create'"),
            nullable=False,
        ),
        sa.Column(
            "linked_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["advisory_id"],
            ["review.funding_advisories.advisory_id"],
            name="fk_funding_link_advisory",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["review.order_proposals.proposal_id"],
            name="fk_funding_link_proposal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", name="uq_funding_link_proposal_id"),
        schema="review",
    )
    op.create_index(
        "ix_funding_link_advisory_id",
        "funding_advisory_proposal_links",
        ["advisory_id"],
        schema="review",
    )

    op.execute(
        """
        CREATE FUNCTION review.reject_funding_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'review.% is append-only; % rejected',
                TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "funding_advisory_revisions",
        "funding_advisory_proposal_links",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON review.{table} FOR EACH ROW EXECUTE "
            "FUNCTION review.reject_funding_evidence_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_truncate_append_only "
            f"BEFORE TRUNCATE ON review.{table} FOR EACH STATEMENT EXECUTE "
            "FUNCTION review.reject_funding_evidence_mutation()"
        )
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON review.{table} FROM PUBLIC")


def downgrade() -> None:
    op.drop_table("funding_advisory_proposal_links", schema="review")
    op.drop_table("funding_advisory_deliveries", schema="review")
    op.drop_table("funding_advisory_revisions", schema="review")
    op.drop_table("funding_advisories", schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.reject_funding_evidence_mutation()")
