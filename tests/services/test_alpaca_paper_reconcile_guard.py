"""ROB-953 — record_status optimistic write guard, proven against real rows.

These are DB-backed on purpose. The round-1 revision of this guard was covered
only by a mock test that compiled the SET clause, so it stayed green while the
WHERE clause silently blocked the legitimate ``anomaly -> canceled`` transition.
Asserting the final persisted state is the only assertion that cannot go
false-green that way.

Guard contract:
  * completed states (position_reconciled / closed / final_reconciled) are
    immutable — a late broker read must not resurrect them;
  * every other state, including the terminal-but-revisable anomaly / canceled /
    filled, stays transitionable;
  * cumulative filled_qty is monotonic — a stale lower reading never overwrites
    a higher persisted one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

pytestmark = [pytest.mark.asyncio]

_COIDS = (
    "rob953-guard-anomaly-cancel",
    "rob953-guard-final-reconciled",
    "rob953-guard-monotonic",
    "rob953-guard-filled-revisable",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(db_session):
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.in_(_COIDS)
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


async def _seed(
    db_session,
    client_order_id: str,
    *,
    lifecycle_state: str,
    filled_qty: Decimal | None = None,
    cancel_status: str | None = None,
) -> AlpacaPaperOrderLedger:
    row = AlpacaPaperOrderLedger(
        client_order_id=client_order_id,
        lifecycle_correlation_id=client_order_id,
        record_kind="execution",
        broker="alpaca",
        account_mode="alpaca_paper",
        lifecycle_state=lifecycle_state,
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        order_type="limit",
        time_in_force="day",
        filled_qty=filled_qty,
        cancel_status=cancel_status,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _state(db_session, client_order_id: str) -> AlpacaPaperOrderLedger:
    result = await db_session.execute(
        select(AlpacaPaperOrderLedger).where(
            AlpacaPaperOrderLedger.client_order_id == client_order_id
        )
    )
    return result.scalar_one()


async def test_anomaly_to_canceled_transition_actually_lands(db_session):
    """The regression the round-1 guard introduced: a real anomaly row given
    cancel evidence must end up canceled, not silently unchanged."""
    coid = "rob953-guard-anomaly-cancel"
    await _seed(db_session, coid, lifecycle_state="anomaly", cancel_status="canceled")

    await AlpacaPaperLedgerService(db_session).record_status(
        coid, {"id": "b1", "status": "canceled", "filled_qty": "0"}
    )

    assert (await _state(db_session, coid)).lifecycle_state == "canceled"


async def test_filled_row_remains_transitionable_on_contradicting_evidence(db_session):
    """``filled`` is terminal but not completed — it must stay revisable."""
    coid = "rob953-guard-filled-revisable"
    await _seed(db_session, coid, lifecycle_state="filled", filled_qty=Decimal("0.014"))

    await AlpacaPaperLedgerService(db_session).record_status(
        coid, {"id": "b1", "status": "rejected", "filled_qty": "0.014"}
    )

    assert (await _state(db_session, coid)).lifecycle_state == "anomaly"


async def test_completed_row_is_never_resurrected_by_a_late_status_write(db_session):
    """final_reconciled has already been consumed downstream — it is immutable."""
    coid = "rob953-guard-final-reconciled"
    await _seed(
        db_session,
        coid,
        lifecycle_state="final_reconciled",
        filled_qty=Decimal("0.014"),
    )

    await AlpacaPaperLedgerService(db_session).record_status(
        coid, {"id": "b1", "status": "filled", "filled_qty": "0.014"}
    )

    row = await _state(db_session, coid)
    assert row.lifecycle_state == "final_reconciled"
    # the rejected write must not leave a raw_responses breadcrumb either
    assert not (row.raw_responses or {})


async def test_stale_lower_filled_qty_never_overwrites_a_higher_one(db_session):
    """Concurrent partial reconciles: 0.8 lands, a stale 0.7 must be ignored."""
    coid = "rob953-guard-monotonic"
    await _seed(db_session, coid, lifecycle_state="submitted")
    svc = AlpacaPaperLedgerService(db_session)

    await svc.record_status(
        coid, {"id": "b1", "status": "partially_filled", "filled_qty": "0.8"}
    )
    assert (await _state(db_session, coid)).filled_qty == Decimal("0.8")

    # stale reader replays an older, lower cumulative quantity
    await svc.record_status(
        coid, {"id": "b1", "status": "partially_filled", "filled_qty": "0.7"}
    )

    row = await _state(db_session, coid)
    assert row.filled_qty == Decimal("0.8"), "stale quantity regressed the row"


async def test_equal_and_increasing_filled_qty_still_apply(db_session):
    """Monotonicity must not block idempotent replay or genuine progress."""
    coid = "rob953-guard-monotonic"
    await _seed(db_session, coid, lifecycle_state="submitted")
    svc = AlpacaPaperLedgerService(db_session)

    await svc.record_status(
        coid, {"id": "b1", "status": "partially_filled", "filled_qty": "0.7"}
    )
    await svc.record_status(
        coid, {"id": "b1", "status": "partially_filled", "filled_qty": "0.7"}
    )
    assert (await _state(db_session, coid)).filled_qty == Decimal("0.7")

    await svc.record_status(coid, {"id": "b1", "status": "filled", "filled_qty": "0.9"})
    row = await _state(db_session, coid)
    assert row.filled_qty == Decimal("0.9")
    assert row.lifecycle_state == "filled"
