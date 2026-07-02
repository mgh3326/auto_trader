"""ROB-653 P6-B — order ledger approval_hash/idempotency_key + order_send_intents

Revision ID: 20260702_rob653
Revises: 20260702_rob651
Create Date: 2026-07-02

Additive: nullable approval_hash/idempotency_key on kis_live_order_ledger and
live_order_ledger, plus review.order_send_intents (KIS pre-send reservation with
UNIQUE(account_scope, idempotency_key)). No changes to reconcile behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260702_rob653"
down_revision: str | Sequence[str] | None = "20260702_rob651"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGERS = ("kis_live_order_ledger", "live_order_ledger")


def upgrade() -> None:
    for table in _LEDGERS:
        op.add_column(table, sa.Column("approval_hash", sa.Text(), nullable=True), schema="review")
        op.add_column(table, sa.Column("idempotency_key", sa.Text(), nullable=True), schema="review")

    op.create_table(
        "order_send_intents",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("account_scope", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("side", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_scope",
            "idempotency_key",
            name="uq_order_send_intent_scope_key",
        ),
        schema="review",
    )


def downgrade() -> None:
    op.drop_table("order_send_intents", schema="review")
    for table in _LEDGERS:
        op.drop_column(table, "idempotency_key", schema="review")
        op.drop_column(table, "approval_hash", schema="review")
