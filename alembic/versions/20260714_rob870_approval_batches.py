"""ROB-870 durable Telegram approval batches.

Revision ID: 20260714_rob870_approval_batches
Revises: 20260713_rob848_paper_validation
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260714_rob870_approval_batches"
down_revision = "20260713_rob848_paper_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_proposal_approval_batches",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column(
            "window_started_at", sa.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("window_closes_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approval_nonce", sa.Text(), nullable=False),
        sa.Column(
            "approval_nonce_used_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("approved_by_telegram_user_id", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("summary_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "summary_dispatch_state",
            sa.Text(),
            nullable=False,
            server_default="idle",
        ),
        sa.Column(
            "summary_dispatch_lease_until",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_order_proposal_approval_batches")
        ),
        sa.UniqueConstraint(
            "batch_id", name="uq_order_proposal_approval_batches_batch_id"
        ),
        sa.CheckConstraint(
            "summary_dispatch_state IN ('idle','sending','sent')",
            name="order_proposal_approval_batches_summary_state",
        ),
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_approval_batches_chat_window",
        "order_proposal_approval_batches",
        ["chat_id", "window_closes_at"],
        schema="review",
    )

    op.create_table(
        "order_proposal_approval_batch_members",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("batch_pk", sa.BigInteger(), nullable=False),
        sa.Column("proposal_pk", sa.BigInteger(), nullable=False),
        sa.Column("approval_nonce_snapshot", sa.Text(), nullable=False),
        sa.Column("approval_message_id", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column(
            "result_detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("added_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name=op.f("pk_order_proposal_approval_batch_members")
        ),
        sa.ForeignKeyConstraint(
            ["batch_pk"],
            ["review.order_proposal_approval_batches.id"],
            name="fk_order_proposal_batch_members_batch",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_pk"],
            ["review.order_proposals.id"],
            name="fk_order_proposal_batch_members_proposal",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "batch_pk", "proposal_pk", name="uq_order_proposal_batch_member"
        ),
        sa.UniqueConstraint(
            "proposal_pk",
            "approval_nonce_snapshot",
            name="uq_order_proposal_batch_member_nonce",
        ),
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_batch_members_batch_pk",
        "order_proposal_approval_batch_members",
        ["batch_pk"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_order_proposal_batch_members_batch_pk",
        table_name="order_proposal_approval_batch_members",
        schema="review",
    )
    op.drop_table("order_proposal_approval_batch_members", schema="review")
    op.drop_index(
        "ix_order_proposal_approval_batches_chat_window",
        table_name="order_proposal_approval_batches",
        schema="review",
    )
    op.drop_table("order_proposal_approval_batches", schema="review")
