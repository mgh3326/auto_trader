"""ROB-1440: execution intents and the child request write-ahead journal.

Additive: two new tables, no change to any existing table.

Two layers protect the rows. The app role gets no DELETE on either table and
UPDATE on the journal's outcome columns only. On intents it gets UPDATE on ``id``
alone, because the gateway's SELECT ... FOR UPDATE needs UPDATE on at least one
column; the trigger still refuses any real update. Triggers hold
the same rules for any role, including before the #789 Stage 4 role split: intents
are immutable, journal request fields are immutable, a journal row moves only from
``sending`` to ``accepted`` or ``uncertain``, a new journal row starts as
``sending`` or ``refused`` with no outcome, and no row is deleted. Rewriting a
``sending`` row's key would let a retry look new and send twice, and inserting an
``accepted`` row would hand a later cancel a forged broker order id.

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

#: One statement per entry. The DB test installs these so it exercises this DDL.
GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.execution_intents_immutable()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'review.execution_intents rows are immutable'
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_intents_immutable "
    "ON review.execution_intents",
    """
    CREATE TRIGGER trg_execution_intents_immutable
    BEFORE UPDATE OR DELETE ON review.execution_intents
    FOR EACH ROW EXECUTE FUNCTION review.execution_intents_immutable()
    """,
    """
    CREATE OR REPLACE FUNCTION review.execution_child_requests_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'review.execution_child_requests rows cannot be deleted'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.state NOT IN ('sending', 'refused')
               OR NEW.broker_order_id IS NOT NULL
               OR NEW.result IS NOT NULL THEN
                RAISE EXCEPTION 'review.execution_child_requests new rows cannot carry an outcome'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.state <> 'sending' OR NEW.state NOT IN ('accepted', 'uncertain') THEN
            RAISE EXCEPTION 'review.execution_child_requests state change % to % refused',
                OLD.state, NEW.state USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.intent_id IS DISTINCT FROM OLD.intent_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
           OR NEW.action IS DISTINCT FROM OLD.action
           OR NEW.target_idempotency_key IS DISTINCT FROM OLD.target_idempotency_key
           OR NEW.quantity IS DISTINCT FROM OLD.quantity
           OR NEW.limit_price_krw IS DISTINCT FROM OLD.limit_price_krw
           OR NEW.refusal_reason IS DISTINCT FROM OLD.refusal_reason
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'review.execution_child_requests request fields are immutable'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_child_requests_guard "
    "ON review.execution_child_requests",
    """
    CREATE TRIGGER trg_execution_child_requests_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.execution_child_requests
    FOR EACH ROW EXECUTE FUNCTION review.execution_child_requests_guard()
    """,
)


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
            "schema_version = 1", name=op.f("ck_execution_intents_schema_version")
        ),
        sa.CheckConstraint(
            "account IN ('kis_mock')",
            name=op.f("ck_execution_intents_account_mock_only"),
        ),
        sa.CheckConstraint("venue IN ('krx')", name=op.f("ck_execution_intents_venue")),
        sa.CheckConstraint(
            "side IN ('buy', 'sell')", name=op.f("ck_execution_intents_side")
        ),
        sa.CheckConstraint(
            "max_quantity > 0", name=op.f("ck_execution_intents_max_quantity")
        ),
        sa.CheckConstraint(
            "max_notional_krw > 0", name=op.f("ck_execution_intents_max_notional")
        ),
        sa.CheckConstraint(
            "limit_price_krw > 0", name=op.f("ck_execution_intents_limit_price")
        ),
        sa.CheckConstraint(
            "max_slippage_bps BETWEEN 0 AND 500",
            name=op.f("ck_execution_intents_slippage"),
        ),
        sa.CheckConstraint(
            "position_at_authorization >= 0 AND min_position_after >= 0 "
            "AND min_position_after <= max_position_after",
            name=op.f("ck_execution_intents_inventory"),
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
            name=op.f("ck_execution_child_requests_action"),
        ),
        sa.CheckConstraint(
            "state IN ('sending', 'accepted', 'uncertain', 'refused')",
            name=op.f("ck_execution_child_requests_state"),
        ),
        sa.CheckConstraint(
            "(state = 'refused') = (refusal_reason IS NOT NULL)",
            name=op.f("ck_execution_child_requests_refusal"),
        ),
        sa.CheckConstraint(
            "(state = 'sending') = (finished_at IS NULL)",
            name=op.f("ck_execution_child_requests_finished_at"),
        ),
        sa.CheckConstraint(
            "(action = 'submit') = (target_idempotency_key IS NULL)",
            name=op.f("ck_execution_child_requests_target"),
        ),
        sa.CheckConstraint(
            "(action = 'cancel' AND quantity IS NULL AND limit_price_krw IS NULL) "
            "OR (action <> 'cancel' AND quantity > 0 AND limit_price_krw > 0)",
            name=op.f("ck_execution_child_requests_order_values"),
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
    for statement in GUARD_DDL:
        op.execute(statement)
    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.execution_intents TO at_app; "
        "GRANT UPDATE (id) ON review.execution_intents TO at_app; "
        "GRANT SELECT, INSERT ON review.execution_child_requests TO at_app; "
        "GRANT UPDATE (state, broker_order_id, result, finished_at) "
        "ON review.execution_child_requests TO at_app; "
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
    op.execute("DROP FUNCTION IF EXISTS review.execution_child_requests_guard()")
    op.execute("DROP FUNCTION IF EXISTS review.execution_intents_immutable()")
