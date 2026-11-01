"""ROB-1440: execution intents and the child request write-ahead journal.

Additive: two new tables, no change to any existing table. The app role gets no
DELETE on either table and no UPDATE on intents, so intents stay immutable and a
journal row that blocks a resend cannot be removed by the app.

Revision ID: 20261101_rob1440_intent_gw
Revises: 20261016_rob1378_q46_lock
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261101_rob1440_intent_gw"
down_revision: str | Sequence[str] | None = "20261016_rob1378_q46_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("strategy", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("max_quantity", sa.BigInteger(), nullable=False),
        sa.Column("max_notional_krw", sa.BigInteger(), nullable=False),
        sa.Column("limit_price_krw", sa.BigInteger(), nullable=False),
        sa.Column("max_slippage_bps", sa.Integer(), nullable=False),
        sa.Column("position_at_authorization", sa.BigInteger(), nullable=False),
        sa.Column("min_position_after", sa.BigInteger(), nullable=False),
        sa.Column("max_position_after", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("authorization_ref", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("intent_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version = 1", name="ck_execution_intents_schema_version"
        ),
        sa.CheckConstraint(
            "account IN ('kis_mock')", name="ck_execution_intents_account_mock_only"
        ),
        sa.CheckConstraint("venue IN ('krx')", name="ck_execution_intents_venue"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_execution_intents_side"),
        sa.CheckConstraint(
            "max_quantity > 0", name="ck_execution_intents_max_quantity"
        ),
        sa.CheckConstraint(
            "max_notional_krw > 0", name="ck_execution_intents_max_notional"
        ),
        sa.CheckConstraint(
            "limit_price_krw > 0", name="ck_execution_intents_limit_price"
        ),
        sa.CheckConstraint(
            "max_slippage_bps BETWEEN 0 AND 500", name="ck_execution_intents_slippage"
        ),
        sa.CheckConstraint(
            "position_at_authorization >= 0 AND min_position_after >= 0 "
            "AND min_position_after <= max_position_after",
            name="ck_execution_intents_inventory",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_intents"),
        schema="review",
    )
    op.create_table(
        "execution_child_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("intent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_idempotency_key", sa.Text(), nullable=True),
        sa.Column("quantity", sa.BigInteger(), nullable=True),
        sa.Column("limit_price_krw", sa.BigInteger(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('submit', 'amend', 'cancel')",
            name="ck_execution_child_requests_action",
        ),
        sa.CheckConstraint(
            "state IN ('sending', 'accepted', 'uncertain', 'refused')",
            name="ck_execution_child_requests_state",
        ),
        sa.CheckConstraint(
            "(state = 'refused') = (refusal_reason IS NOT NULL)",
            name="ck_execution_child_requests_refusal",
        ),
        sa.CheckConstraint(
            "(state = 'sending') = (finished_at IS NULL)",
            name="ck_execution_child_requests_finished_at",
        ),
        sa.CheckConstraint(
            "(action = 'submit') = (target_idempotency_key IS NULL)",
            name="ck_execution_child_requests_target",
        ),
        sa.CheckConstraint(
            "(action = 'cancel' AND quantity IS NULL AND limit_price_krw IS NULL) "
            "OR (action <> 'cancel' AND quantity > 0 AND limit_price_krw > 0)",
            name="ck_execution_child_requests_order_values",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            ["review.execution_intents.id"],
            name="fk_execution_child_requests_intent_id_execution_intents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_execution_child_requests"),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_child_requests_key"),
        schema="review",
    )
    op.create_index(
        "ix_execution_child_requests_intent",
        "execution_child_requests",
        ["intent_id", "id"],
        schema="review",
    )
    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.execution_intents TO at_app; "
        "GRANT SELECT, INSERT, UPDATE ON review.execution_child_requests TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE review.execution_child_requests_id_seq "
        "TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_child_requests_intent",
        table_name="execution_child_requests",
        schema="review",
    )
    op.drop_table("execution_child_requests", schema="review")
    op.drop_table("execution_intents", schema="review")
