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
from sqlalchemy import delete, select, update

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

pytestmark = [pytest.mark.asyncio]

_COIDS = (
    "rob953-guard-e2e",
    "rob953-guard-sibling-rows",
    "rob953-guard-anomaly-cancel",
    "rob953-guard-final-reconciled",
    "rob953-guard-monotonic",
    "rob953-guard-filled-revisable",
    "rob953-guard-final-race",
    "rob953-guard-monotonic-race",
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


# ---------------------------------------------------------------------------
# End-to-end: the real reconcile service against the real ledger service.
# The unit suite drives a fake ledger, so this is the only place the two real
# implementations meet — which is where record_status' return-row semantics bite.
# ---------------------------------------------------------------------------


class _FakeBroker:
    def __init__(self, order):
        self._order = order

    async def get_order_by_client_order_id(self, _):
        return self._order

    async def list_fills(self, **_):
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                id="fill-1",
                order_id=self._order.id,
                qty=self._order.filled_qty,
                price=self._order.filled_avg_price,
                cum_qty=self._order.filled_qty,
            )
        ]


class _CompetingBroker(_FakeBroker):
    """Commits a competing lifecycle write after reconcile loaded its stale row."""

    def __init__(
        self,
        order,
        *,
        client_order_id: str,
        lifecycle_state: str,
        filled_qty: str,
    ) -> None:
        super().__init__(order)
        self._client_order_id = client_order_id
        self._lifecycle_state = lifecycle_state
        self._filled_qty = Decimal(filled_qty)

    async def get_order_by_client_order_id(self, _):
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as competing_session:
            await competing_session.execute(
                update(AlpacaPaperOrderLedger)
                .where(
                    AlpacaPaperOrderLedger.client_order_id == self._client_order_id,
                    AlpacaPaperOrderLedger.record_kind == "execution",
                )
                .values(
                    lifecycle_state=self._lifecycle_state,
                    filled_qty=self._filled_qty,
                    filled_avg_price=Decimal("353.156"),
                )
            )
            await competing_session.commit()
        return self._order


async def _fresh_state(client_order_id: str) -> AlpacaPaperOrderLedger:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as fresh_session:
        result = await fresh_session.execute(
            select(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.client_order_id == client_order_id,
                AlpacaPaperOrderLedger.record_kind == "execution",
            )
        )
        return result.scalar_one()


def _order(status: str = "filled", filled_qty: str = "1"):
    from types import SimpleNamespace

    return SimpleNamespace(
        id="broker-order-1",
        client_order_id="rob953-guard-sibling-rows",
        qty=Decimal("1"),
        filled_qty=Decimal(filled_qty),
        filled_avg_price=Decimal("353.156"),
        status=status,
    )


async def test_reconcile_books_through_the_real_ledger_service(db_session):
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    coid = "rob953-guard-e2e"
    await _seed(db_session, coid, lifecycle_state="submitted")

    result = await AlpacaPaperReconcileService(
        AlpacaPaperLedgerService(db_session), _FakeBroker(_order())
    ).reconcile(client_order_id=coid, dry_run=False)

    outcome = result["reconciled"][0]
    row = await _state(db_session, coid)
    assert outcome["action"] == "booked_filled"
    assert row.lifecycle_state == "filled"
    assert row.filled_qty == Decimal("1")
    # a successful booking must not be flagged as rejected
    assert outcome.get("stale_write_rejected") is not True
    assert outcome.get("requires_manual_review") is not True
    assert outcome["persisted_lifecycle_state"] == "filled"


async def test_sibling_preview_row_does_not_fake_a_rejected_write(db_session):
    """A newer non-execution row sharing the client_order_id must not be mistaken
    for the execution row when confirming what was persisted."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    coid = "rob953-guard-sibling-rows"
    await _seed(db_session, coid, lifecycle_state="submitted")
    # a preview row created *after* the execution row, same client_order_id
    sibling = AlpacaPaperOrderLedger(
        client_order_id=coid,
        lifecycle_correlation_id=coid,
        record_kind="preview",
        broker="alpaca",
        account_mode="alpaca_paper",
        lifecycle_state="previewed",
        execution_symbol="ISRG",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.equity_us,
        side="buy",
        order_type="limit",
        time_in_force="day",
    )
    db_session.add(sibling)
    await db_session.commit()

    result = await AlpacaPaperReconcileService(
        AlpacaPaperLedgerService(db_session), _FakeBroker(_order())
    ).reconcile(client_order_id=coid, dry_run=False)

    outcome = result["reconciled"][0]
    execution = (
        await db_session.execute(
            select(AlpacaPaperOrderLedger).where(
                AlpacaPaperOrderLedger.client_order_id == coid,
                AlpacaPaperOrderLedger.record_kind == "execution",
            )
        )
    ).scalar_one()

    assert execution.lifecycle_state == "filled"
    assert outcome["persisted_lifecycle_state"] == "filled"
    assert outcome.get("stale_write_rejected") is not True
    assert outcome.get("requires_manual_review") is not True


async def test_reconcile_response_reflects_competing_final_state(db_session):
    """A rejected write must not report booked_filled from a stale identity map."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    coid = "rob953-guard-final-race"
    await _seed(
        db_session,
        coid,
        lifecycle_state="submitted",
        filled_qty=Decimal("0"),
    )
    broker = _CompetingBroker(
        _order(status="filled", filled_qty="1"),
        client_order_id=coid,
        lifecycle_state="final_reconciled",
        filled_qty="0.8",
    )

    result = await AlpacaPaperReconcileService(
        AlpacaPaperLedgerService(db_session), broker
    ).reconcile(client_order_id=coid, dry_run=False)

    outcome = result["reconciled"][0]
    persisted = await _fresh_state(coid)
    assert persisted.lifecycle_state == "final_reconciled"
    assert persisted.filled_qty == Decimal("0.8")
    assert outcome["action"] == "noop_stale_write_rejected"
    assert outcome["lifecycle_state"] == "final_reconciled"
    assert outcome["persisted_lifecycle_state"] == "final_reconciled"
    assert outcome["filled_qty"] == "0.8"
    assert outcome["stale_write_rejected"] is True


async def test_reconcile_response_reflects_competing_higher_quantity(db_session):
    """A stale 0.7 attempt must report the already-persisted 0.8, not a booking."""
    from app.services.alpaca_paper_reconcile_service import AlpacaPaperReconcileService

    coid = "rob953-guard-monotonic-race"
    await _seed(
        db_session,
        coid,
        lifecycle_state="submitted",
        filled_qty=Decimal("0"),
    )
    broker = _CompetingBroker(
        _order(status="partially_filled", filled_qty="0.7"),
        client_order_id=coid,
        lifecycle_state="submitted",
        filled_qty="0.8",
    )

    result = await AlpacaPaperReconcileService(
        AlpacaPaperLedgerService(db_session), broker
    ).reconcile(client_order_id=coid, dry_run=False)

    outcome = result["reconciled"][0]
    persisted = await _fresh_state(coid)
    assert persisted.lifecycle_state == "submitted"
    assert persisted.filled_qty == Decimal("0.8")
    assert outcome["action"] == "noop_stale_write_rejected"
    assert outcome["lifecycle_state"] == "submitted"
    assert outcome["persisted_lifecycle_state"] == "submitted"
    assert outcome["filled_qty"] == "0.8"
    assert outcome["stale_write_rejected"] is True
