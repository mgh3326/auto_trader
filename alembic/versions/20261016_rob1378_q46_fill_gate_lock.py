"""ROB-1378: serialize the Q-46 COMMIT-time fill gate with kis/mock fill writers.

#1250 left a window: the deferred constraint trigger
``trg_kis_mock_inference_requires_audit`` re-reads every fill source while the
close COMMITs, but nothing ordered it against a fill writer. A fill whose
transaction was in flight (written but not yet committed) when the trigger's
statements ran stayed invisible to them, and both transactions then committed:
the row closed as ``expired[inference]`` next to a recorded fill.

This revision closes the window with one transaction-scoped advisory lock key
shared by both sides:

* ``review.kis_mock_q46_fill_writer_lock()`` — a BEFORE INSERT OR UPDATE row
  trigger on ``review.execution_ledger`` (kis/mock rows only) and on
  ``review.kis_mock_order_ledger`` (rows whose new image carries fill evidence,
  the same predicate the gate reads) takes the key SHARED and keeps it to the
  writer's transaction end. Writers never wait for each other; nothing is
  refused or rewritten.
* ``review.require_kis_mock_inference_audit()`` (replaced) takes the key
  EXCLUSIVE right before the fill checks. An in-flight fill transaction ends
  first and the checks (fresh READ COMMITTED snapshot per statement) see it if
  it committed; a fill writer that arrives later waits until the close has
  committed or rolled back, so it is recorded after the close, exactly like
  any later fill. A close running under any isolation level other than READ
  COMMITTED is refused (a fixed snapshot would make the wait useless).

No table, column or row is touched; the kis_mock rows are still closed only by
the operator's CLI. Locking: CREATE TRIGGER on review.execution_ledger and
review.kis_mock_order_ledger (SHARE ROW EXCLUSIVE, brief). Apply outside KRX
hours like other ledger DDL.

Revision ID: 20261016_rob1378_q46_lock
Revises: 20261005_t1250_kismock_inf
Create Date: 2026-10-16
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20261016_rob1378_q46_lock"
down_revision: str | Sequence[str] | None = "20261005_t1250_kismock_inf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The one advisory key both sides use (``hashtextextended(<text>, 0)``).
FILL_GATE_LOCK_KEY_TEXT = "auto_trader:kis_mock_q46_fill_gate"

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
    -- Last fill gate, evaluated at COMMIT (deferred). ROB-1378: it is
    -- serialized with every kis/mock fill writer. Each writer takes the
    -- shared fill-gate lock in a BEFORE trigger
    -- (review.kis_mock_q46_fill_writer_lock) and holds it to its own
    -- transaction end; this close takes the same key exclusively first. A
    -- fill transaction already in flight therefore finishes before the
    -- checks below run, and each check takes a fresh READ COMMITTED snapshot,
    -- so it sees that fill if it committed; a fill writer arriving later
    -- blocks until this close has committed or rolled back. The fresh
    -- per-statement snapshot is what makes the wait useful, so any other
    -- isolation level is refused. Quarantined execution-ledger rows count
    -- (the safe direction).
    IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
        RAISE EXCEPTION 'review.kis_mock_order_ledger row %: Q-46 inference close requires READ COMMITTED (got %)',
            NEW.id, current_setting('transaction_isolation') USING ERRCODE = 'check_violation';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('auto_trader:kis_mock_q46_fill_gate', 0));
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
FILL_WRITER_LOCK_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.kis_mock_q46_fill_writer_lock()
RETURNS trigger AS $$
BEGIN
    -- ROB-1378: shared side of the Q-46 fill-gate lock, held to the end of
    -- the writing transaction. Never refuses or changes the row.
    PERFORM pg_advisory_xact_lock_shared(
        hashtextextended('auto_trader:kis_mock_q46_fill_gate', 0));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

EXECUTION_LEDGER_LOCK_TRIGGER_DDL = """
CREATE TRIGGER trg_execution_ledger_kis_mock_q46_fill_lock
BEFORE INSERT OR UPDATE ON review.execution_ledger
FOR EACH ROW
WHEN (NEW.broker = 'kis' AND NEW.account_mode = 'mock')
EXECUTE FUNCTION review.kis_mock_q46_fill_writer_lock()
"""

MOCK_LEDGER_LOCK_TRIGGER_DDL = """
CREATE TRIGGER trg_kis_mock_order_ledger_q46_fill_lock
BEFORE INSERT OR UPDATE ON review.kis_mock_order_ledger
FOR EACH ROW
WHEN (
    NEW.lifecycle_state IN ('fill', 'reconciled')
    OR review.kis_mock_q46_detail_has_fill(NEW.last_reconcile_detail)
)
EXECUTE FUNCTION review.kis_mock_q46_fill_writer_lock()
"""


def _t1250_require_audit_ddl() -> str:
    """The #1250 function body, restored verbatim on downgrade."""
    path = Path(__file__).with_name("20261005_t1250_kismock_inference.py")
    spec = importlib.util.spec_from_file_location("_t1250_for_rob1378", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.REQUIRE_AUDIT_FUNCTION_DDL


def upgrade() -> None:
    op.execute(REQUIRE_AUDIT_FUNCTION_DDL)
    op.execute(FILL_WRITER_LOCK_FUNCTION_DDL)
    op.execute(EXECUTION_LEDGER_LOCK_TRIGGER_DDL)
    op.execute(MOCK_LEDGER_LOCK_TRIGGER_DDL)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_kis_mock_order_ledger_q46_fill_lock "
        "ON review.kis_mock_order_ledger"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_execution_ledger_kis_mock_q46_fill_lock "
        "ON review.execution_ledger"
    )
    op.execute("DROP FUNCTION IF EXISTS review.kis_mock_q46_fill_writer_lock()")
    op.execute(_t1250_require_audit_ddl())
