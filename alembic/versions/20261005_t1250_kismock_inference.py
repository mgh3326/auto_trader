"""#1250 append-only audit table for the Q-46 kis_mock expired[inference] close.

Creates ``review.kis_mock_inference_expiry_events``: one row per kis_mock
ledger row closed by ``scripts/expire_kis_mock_rows_by_inference.py``
(``ledger_id`` UNIQUE, CHECK confines it to the decision's four ids 63/64/66/80,
fixed action / decision ref / after state), and triggers that reject UPDATE,
DELETE and TRUNCATE. Two more triggers couple a close and its audit row:

* BEFORE INSERT on the audit table: the ledger row must already be
  ``expired`` with this rule's marker, decision ref Q-46 and the same batch id
  (no orphan audit row).
* a DEFERRABLE INITIALLY DEFERRED constraint trigger AFTER UPDATE on
  ``review.kis_mock_order_ledger``, firing only for rows whose detail carries
  this rule's marker: the row must be one of the four ids, the transition must
  be accepted/pending -> expired, and at COMMIT the same-batch audit row must
  exist (no close without its audit; a closed row cannot be rewritten).

No existing column or row is touched; the kis_mock ledger rows themselves are
closed later by the operator's CLI run, not here.

Locking: CREATE TABLE plus CREATE TRIGGER on review.kis_mock_order_ledger
(SHARE ROW EXCLUSIVE, brief; the table is low-volume). Apply outside KRX
hours like other ledger DDL.

Revision ID: 20261005_t1250_kismock_inf
Revises: 20261001_t1175_ledger_quar
Create Date: 2026-10-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261005_t1250_kismock_inf"
down_revision: str | Sequence[str] | None = "20261001_t1175_ledger_quar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER_IDS_SQL = "ledger_id IN (63, 64, 66, 80)"
#: Mirrors app/services/kis_mock_inference_expiry.py (pinned by a test).
INFERENCE_REASON_CODE = "expired_inference:kis_regular_day_order_no_broker_original"
INFERENCE_RULE_ID = "kis_mock_regular_day_leftover_expired_inference_q46"

REQUIRE_CLOSE_FUNCTION_DDL = f"""
CREATE OR REPLACE FUNCTION review.require_kis_mock_inference_close()
RETURNS trigger AS $$
DECLARE
    v_state text;
    v_detail jsonb;
BEGIN
    SELECT lifecycle_state, last_reconcile_detail INTO v_state, v_detail
      FROM review.kis_mock_order_ledger WHERE id = NEW.ledger_id;
    IF v_state IS DISTINCT FROM 'expired'
       OR v_detail->>'reason_code' IS DISTINCT FROM '{INFERENCE_REASON_CODE}'
       OR v_detail->>'inference_rule' IS DISTINCT FROM '{INFERENCE_RULE_ID}'
       OR v_detail->>'operator_decision_ref' IS DISTINCT FROM 'Q-46'
       OR v_detail->>'batch_id' IS DISTINCT FROM NEW.batch_id::text THEN
        RAISE EXCEPTION 'review.kis_mock_inference_expiry_events: ledger row % is not closed by batch %',
            NEW.ledger_id, NEW.batch_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

REQUIRE_AUDIT_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.require_kis_mock_inference_audit()
RETURNS trigger AS $$
BEGIN
    IF NEW.id NOT IN (63, 64, 66, 80)
       OR NEW.lifecycle_state IS DISTINCT FROM 'expired'
       OR OLD.lifecycle_state NOT IN ('accepted', 'pending') THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: Q-46 inference close outside its contract',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM review.kis_mock_inference_expiry_events e
         WHERE e.ledger_id = NEW.id
           AND e.batch_id::text = NEW.last_reconcile_detail->>'batch_id'
    ) THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: Q-46 inference close without its audit row',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

REQUIRE_AUDIT_TRIGGER_DDL = f"""
CREATE CONSTRAINT TRIGGER trg_kis_mock_inference_requires_audit
AFTER UPDATE ON review.kis_mock_order_ledger
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (
    NEW.last_reconcile_detail->>'reason_code' = '{INFERENCE_REASON_CODE}'
    OR NEW.last_reconcile_detail->>'inference_rule' = '{INFERENCE_RULE_ID}'
)
EXECUTE FUNCTION review.require_kis_mock_inference_audit()
"""

AUDIT_REJECT_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.reject_kis_mock_inference_expiry_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'review.% is append-only; % rejected',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "kis_mock_inference_expiry_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("operator_decision_ref", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("before_state", sa.Text(), nullable=False),
        sa.Column("after_state", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kis_mock_inference_expiry_events")),
        sa.UniqueConstraint(
            "ledger_id", name=op.f("uq_kis_mock_inference_expiry_ledger")
        ),
        sa.CheckConstraint(
            LEDGER_IDS_SQL,
            name=op.f("ck_kis_mock_inference_expiry_events_ledger_id"),
        ),
        sa.CheckConstraint(
            "action = 'expire_inference'",
            name=op.f("ck_kis_mock_inference_expiry_events_action"),
        ),
        sa.CheckConstraint(
            "operator_decision_ref = 'Q-46'",
            name=op.f("ck_kis_mock_inference_expiry_events_decision_ref"),
        ),
        sa.CheckConstraint(
            "after_state = 'expired'",
            name=op.f("ck_kis_mock_inference_expiry_events_after_state"),
        ),
        sa.CheckConstraint(
            "before_state IN ('accepted', 'pending')",
            name=op.f("ck_kis_mock_inference_expiry_events_before_state"),
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name=op.f("ck_kis_mock_inference_expiry_events_reason_nonblank"),
        ),
        sa.CheckConstraint(
            "btrim(actor) <> ''",
            name=op.f("ck_kis_mock_inference_expiry_events_actor_nonblank"),
        ),
        schema="review",
    )
    op.create_index(
        "ix_kis_mock_inference_expiry_events_batch",
        "kis_mock_inference_expiry_events",
        ["batch_id"],
        schema="review",
    )
    op.execute(AUDIT_REJECT_FUNCTION_DDL)
    op.execute(
        "CREATE TRIGGER trg_kis_mock_inference_expiry_events_append_only "
        "BEFORE UPDATE OR DELETE ON review.kis_mock_inference_expiry_events "
        "FOR EACH ROW EXECUTE FUNCTION "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_kis_mock_inference_expiry_events_truncate "
        "BEFORE TRUNCATE ON review.kis_mock_inference_expiry_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )
    op.execute(REQUIRE_CLOSE_FUNCTION_DDL)
    op.execute(
        "CREATE TRIGGER trg_kis_mock_inference_expiry_events_require_close "
        "BEFORE INSERT ON review.kis_mock_inference_expiry_events "
        "FOR EACH ROW EXECUTE FUNCTION review.require_kis_mock_inference_close()"
    )
    op.execute(REQUIRE_AUDIT_FUNCTION_DDL)
    op.execute(REQUIRE_AUDIT_TRIGGER_DDL)

    # Stage-4 role may not exist in dev/CI databases — conditional GRANT.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.kis_mock_inference_expiry_events "
        "TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE "
        "review.kis_mock_inference_expiry_events_id_seq TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_requires_audit "
        "ON review.kis_mock_order_ledger"
    )
    op.execute("DROP FUNCTION IF EXISTS review.require_kis_mock_inference_audit()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_expiry_events_require_close "
        "ON review.kis_mock_inference_expiry_events"
    )
    op.execute("DROP FUNCTION IF EXISTS review.require_kis_mock_inference_close()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_expiry_events_truncate "
        "ON review.kis_mock_inference_expiry_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_expiry_events_append_only "
        "ON review.kis_mock_inference_expiry_events"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.reject_kis_mock_inference_expiry_event_mutation()"
    )
    op.drop_index(
        "ix_kis_mock_inference_expiry_events_batch",
        table_name="kis_mock_inference_expiry_events",
        schema="review",
    )
    op.drop_table("kis_mock_inference_expiry_events", schema="review")
