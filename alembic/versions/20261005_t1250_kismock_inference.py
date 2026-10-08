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
  exist, the batch must have exactly four audit rows, and no fill may be
  recorded for the order, for the symbol at/after the accept instant, or on a
  same-correlation / same-symbol kis_mock row (the last fill gate, evaluated
  at COMMIT).
* a BEFORE UPDATE OR DELETE trigger on ``review.kis_mock_order_ledger`` that
  refuses any change or removal of a row that already carries the marker (a
  closed row is terminal; clearing the marker cannot reopen it).

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

FILL_DETAIL_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.kis_mock_q46_detail_has_fill(d jsonb)
RETURNS boolean AS $$
BEGIN
    -- Mirrors kis_mock_inference_expiry._detail_has_fill_evidence: unknown is
    -- never zero.
    IF d IS NULL OR jsonb_typeof(d) = 'null' THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(d) <> 'object' THEN
        RETURN true;
    END IF;
    IF d->>'reason_code' IN (
        'fill_detected', 'partial_fill_detected', 'position_reconciled',
        'holdings_mismatch', 'attribution_unconfirmed'
    ) THEN
        RETURN true;
    END IF;
    IF d ? 'attributed_fill_qty' THEN
        IF jsonb_typeof(d->'attributed_fill_qty') NOT IN ('string', 'number') THEN
            RETURN true;
        END IF;
        BEGIN
            RETURN (d->>'attributed_fill_qty')::numeric <> 0;
        EXCEPTION WHEN others THEN
            RETURN true;
        END;
    END IF;
    RETURN false;
END;
$$ LANGUAGE plpgsql IMMUTABLE
"""

REQUIRE_AUDIT_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.require_kis_mock_inference_audit()
RETURNS trigger AS $$
DECLARE
    v_accept timestamptz;
    v_time text;
    v_stored timestamptz;
BEGIN
    IF NEW.id NOT IN (63, 64, 66, 80)
       OR NEW.lifecycle_state IS DISTINCT FROM 'expired'
       OR OLD.lifecycle_state NOT IN ('accepted', 'pending')
       OR NEW.trade_date IS DISTINCT FROM OLD.trade_date
       OR NEW.order_time IS DISTINCT FROM OLD.order_time THEN
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
    IF (
        SELECT count(*) FROM review.kis_mock_inference_expiry_events e
         WHERE e.batch_id::text = NEW.last_reconcile_detail->>'batch_id'
    ) <> 4 THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: Q-46 inference close is not a four-row batch',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    -- The fill cutoff comes from the ledger row itself (send-day KST date +
    -- broker ord_tmd, as kis_leftover_inference.resolve_accept_at), never from
    -- the stored detail a caller writes. A detail accept_at that differs from
    -- the row's own instant is refused.
    v_time := NEW.order_time;
    IF v_time ~ '^[0-9]{4}$' THEN
        v_time := v_time || '00';
    END IF;
    v_accept := NULL;
    IF v_time ~ '^[0-9]{6}$'
       AND substr(v_time, 1, 2)::int < 24
       AND substr(v_time, 3, 2)::int < 60
       AND substr(v_time, 5, 2)::int < 60 THEN
        v_accept := (
            (NEW.trade_date AT TIME ZONE 'Asia/Seoul')::date
            + make_time(substr(v_time, 1, 2)::int, substr(v_time, 3, 2)::int,
                        substr(v_time, 5, 2)::int)
        ) AT TIME ZONE 'Asia/Seoul';
    END IF;
    BEGIN
        v_stored := (NEW.last_reconcile_detail->>'accept_at')::timestamptz;
    EXCEPTION WHEN others THEN
        v_stored := NULL;
    END;
    IF v_accept IS NULL OR v_stored IS DISTINCT FROM v_accept THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: Q-46 inference close accept instant does not match the ledger row',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    -- Last fill gate, evaluated at COMMIT (deferred): every statement here
    -- sees every fill committed before it, so a fill committed after the
    -- service's own re-check still refuses the whole close. Quarantined
    -- execution-ledger rows count (the safe direction).
    IF EXISTS (
        SELECT 1 FROM review.execution_ledger x
         WHERE x.broker = 'kis' AND x.account_mode = 'mock'
           AND (x.broker_order_id = NEW.order_no
                OR ltrim(x.broker_order_id, '0') = ltrim(NEW.order_no, '0'))
    ) OR EXISTS (
        SELECT 1 FROM review.execution_ledger x
         WHERE x.broker = 'kis' AND x.account_mode = 'mock'
           AND x.symbol = NEW.symbol AND x.filled_at >= v_accept
    ) OR EXISTS (
        SELECT 1 FROM review.kis_mock_order_ledger s
         WHERE s.id <> NEW.id
           AND (s.lifecycle_state IN ('fill', 'reconciled')
                OR review.kis_mock_q46_detail_has_fill(s.last_reconcile_detail))
           AND (
                (NEW.correlation_id IS NOT NULL
                 AND s.correlation_id = NEW.correlation_id)
                OR (s.symbol = NEW.symbol
                    AND (s.trade_date IS NULL OR s.reconciled_at IS NULL
                         OR s.trade_date >= v_accept OR s.reconciled_at >= v_accept))
           )
    ) THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: fill recorded before COMMIT of the Q-46 inference close',
            NEW.id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

TERMINAL_GUARD_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.guard_kis_mock_inference_closed_row()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'review.kis_mock_order_ledger row % is closed by the Q-46 inference and terminal; % rejected',
        OLD.id, TG_OP USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql
"""

TERMINAL_GUARD_TRIGGER_DDL = f"""
CREATE TRIGGER trg_kis_mock_inference_closed_row_terminal
BEFORE UPDATE OR DELETE ON review.kis_mock_order_ledger
FOR EACH ROW
WHEN (
    OLD.last_reconcile_detail->>'reason_code' = '{INFERENCE_REASON_CODE}'
    OR OLD.last_reconcile_detail->>'inference_rule' = '{INFERENCE_RULE_ID}'
)
EXECUTE FUNCTION review.guard_kis_mock_inference_closed_row()
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
    op.execute(FILL_DETAIL_FUNCTION_DDL)
    op.execute(REQUIRE_AUDIT_FUNCTION_DDL)
    op.execute(REQUIRE_AUDIT_TRIGGER_DDL)
    op.execute(TERMINAL_GUARD_FUNCTION_DDL)
    op.execute(TERMINAL_GUARD_TRIGGER_DDL)

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
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_closed_row_terminal "
        "ON review.kis_mock_order_ledger"
    )
    op.execute("DROP FUNCTION IF EXISTS review.guard_kis_mock_inference_closed_row()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_inference_requires_audit "
        "ON review.kis_mock_order_ledger"
    )
    op.execute("DROP FUNCTION IF EXISTS review.require_kis_mock_inference_audit()")
    op.execute("DROP FUNCTION IF EXISTS review.kis_mock_q46_detail_has_fill(jsonb)")
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
