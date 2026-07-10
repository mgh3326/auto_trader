"""ROB-816 order_proposals + order_proposal_rungs (review schema, additive).

Revision ID: 20260710_rob816_order_proposals
Revises: 20260710_rob800_exit_intent
Create Date: 2026-07-10
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260710_rob816_order_proposals"
down_revision: Union[str, Sequence[str], None] = "20260710_rob800_exit_intent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RUNG_STATES = (
    "draft,pending_approval,revalidating,needs_reconfirm,approved,submitting,"
    "acked,resting,partially_filled,unverified,filled,cancelled,expired,rejected,"
    "voided,voided_local_stale,superseded"
).split(",")
_GROUP_STATES = (
    "proposed,approved,partially_submitted,submitted,terminal,rejected,expired,"
    "voided,superseded"
).split(",")


def _in(col: str, values) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "order_proposals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_proposal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "supersedes_proposal_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "superseded_by_proposal_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "no_resubmit", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("proposer", sa.Text(), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=True),
        sa.Column("strategy", sa.Text(), nullable=True),
        sa.Column(
            "rationale", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("broker_account_id", sa.Text(), nullable=True),
        sa.Column(
            "lot_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "lifecycle_state", sa.Text(), nullable=False, server_default="proposed"
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "source_asof", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("approval_nonce", sa.Text(), nullable=True),
        sa.Column(
            "approval_nonce_used_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("approved_by_telegram_user_id", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("commit_lease_until", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_proposals")),
        sa.UniqueConstraint("proposal_id", name="uq_order_proposals_proposal_id"),
        sa.CheckConstraint(
            _in("market", ["equity_kr", "equity_us", "crypto", "forex", "index"]),
            name="order_proposals_market",
        ),
        sa.CheckConstraint(
            _in(
                "account_mode",
                ["kis_live", "kis_mock", "toss_live", "upbit", "db_simulated"],
            ),
            name="order_proposals_account_mode",
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="order_proposals_side"),
        sa.CheckConstraint(
            "order_type IN ('limit','market')", name="order_proposals_order_type"
        ),
        sa.CheckConstraint(
            _in("lifecycle_state", _GROUP_STATES),
            name="order_proposals_lifecycle_state",
        ),
        schema="review",
    )
    op.create_index(
        "ix_order_proposals_root", "order_proposals", ["root_proposal_id"], schema="review"
    )
    op.create_index(
        "ix_order_proposals_state", "order_proposals", ["lifecycle_state"], schema="review"
    )
    op.create_index(
        "ix_order_proposals_symbol", "order_proposals", ["symbol"], schema="review"
    )

    op.create_table(
        "order_proposal_rungs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_pk", sa.BigInteger(), nullable=False),
        sa.Column("rung_index", sa.Integer(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 12), nullable=False),
        sa.Column("limit_price", sa.Numeric(38, 12), nullable=True),
        sa.Column("notional", sa.Numeric(38, 12), nullable=True),
        sa.Column(
            "state", sa.Text(), nullable=False, server_default="pending_approval"
        ),
        sa.Column("approval_hash_digest", sa.Text(), nullable=True),
        sa.Column("approval_revision", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("filled_qty", sa.Numeric(38, 12), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_proposal_rungs")),
        sa.ForeignKeyConstraint(
            ["proposal_pk"],
            ["review.order_proposals.id"],
            name="fk_order_proposal_rungs_proposal",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "proposal_pk", "rung_index", name="uq_order_proposal_rungs_pk_index"
        ),
        sa.CheckConstraint("side IN ('buy','sell')", name="order_proposal_rungs_side"),
        sa.CheckConstraint(
            _in("state", _RUNG_STATES), name="order_proposal_rungs_state"
        ),
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_rungs_proposal_pk",
        "order_proposal_rungs",
        ["proposal_pk"],
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_rungs_broker_order_id",
        "order_proposal_rungs",
        ["broker_order_id"],
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_rungs_correlation_id",
        "order_proposal_rungs",
        ["correlation_id"],
        schema="review",
    )
    op.create_index(
        "ix_order_proposal_rungs_state",
        "order_proposal_rungs",
        ["state"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_order_proposal_rungs_state",
        table_name="order_proposal_rungs",
        schema="review",
    )
    op.drop_index(
        "ix_order_proposal_rungs_correlation_id",
        table_name="order_proposal_rungs",
        schema="review",
    )
    op.drop_index(
        "ix_order_proposal_rungs_broker_order_id",
        table_name="order_proposal_rungs",
        schema="review",
    )
    op.drop_index(
        "ix_order_proposal_rungs_proposal_pk",
        table_name="order_proposal_rungs",
        schema="review",
    )
    op.drop_table("order_proposal_rungs", schema="review")
    op.drop_index(
        "ix_order_proposals_symbol", table_name="order_proposals", schema="review"
    )
    op.drop_index(
        "ix_order_proposals_state", table_name="order_proposals", schema="review"
    )
    op.drop_index(
        "ix_order_proposals_root", table_name="order_proposals", schema="review"
    )
    op.drop_table("order_proposals", schema="review")
