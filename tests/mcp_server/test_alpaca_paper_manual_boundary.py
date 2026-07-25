"""ROB-842 blocker 1: manual alpaca_paper_submit_order routes every real broker
POST through the durable packet + ledger atomic-claim coordinator.

No direct-POST fallback; exactly-once for duplicate manual intents; behaviour is
independent of the automated feature flag; dry-run mutates nothing.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.alpaca_paper_orders import (
    alpaca_paper_cancel_order,
    alpaca_paper_submit_order,
    reset_alpaca_paper_orders_service_factory,
    set_alpaca_paper_orders_service_factory,
)
from app.models.review import AlpacaPaperOrderLedger
from tests.test_alpaca_paper_orders_tools import FakeOrdersService

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _clean():
    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.like("rob73-%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob74-crypto-%")
    )
    snap_stmt = delete(MarketQuoteSnapshot).where(MarketQuoteSnapshot.symbol == "AAPL")
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.execute(snap_stmt)
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(stmt)
        await db.execute(snap_stmt)
        await db.commit()


async def _seed_snapshot(price: str = "150") -> int:
    from datetime import UTC, datetime, timedelta

    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="AAPL",
            source="yahoo",
            snapshot_at=datetime.now(UTC) - timedelta(seconds=10),
            price=Decimal(price),
        )
        db.add(row)
        await db.commit()
        return row.id


@pytest.fixture
def fake_service() -> FakeOrdersService:
    service = FakeOrdersService()
    set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
    yield service
    reset_alpaca_paper_orders_service_factory()


_BUY = {
    "symbol": "AAPL",
    "side": "buy",
    "type": "limit",
    "qty": Decimal("1"),
    "limit_price": Decimal("1.00"),
}


async def _count_rows(coid_prefix: str) -> int:
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(AlpacaPaperOrderLedger).where(
                        AlpacaPaperOrderLedger.client_order_id.like(f"{coid_prefix}%")
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


async def test_manual_confirm_goes_through_claim_exactly_once_sequential(fake_service):
    sid = await _seed_snapshot()
    first = await alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True)
    second = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )

    assert first["submitted"] is True
    assert second["submitted"] is False
    assert second["status"] in {"replayed", "recovered"}
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1


async def test_manual_confirm_exactly_once_parallel(fake_service):
    sid = await _seed_snapshot()
    results = await asyncio.gather(
        alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True),
        alpaca_paper_submit_order(**_BUY, quote_snapshot_id=sid, confirm=True),
    )
    submit_calls = [c for c in fake_service.calls if c[0] == "submit_order"]
    assert len(submit_calls) == 1
    assert sum(1 for r in results if r["submitted"]) == 1


async def test_manual_works_with_automated_flag_off(fake_service, monkeypatch):
    monkeypatch.setattr(settings, "alpaca_paper_automated_submit_enabled", False)
    sid = await _seed_snapshot()
    payload = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )
    # Manual path does not depend on the automated flag; it still routes through
    # the claim and POSTs exactly once.
    assert payload["submitted"] is True
    assert len([c for c in fake_service.calls if c[0] == "submit_order"]) == 1
    assert await _count_rows("rob73-") == 1  # a claim/execution row exists


async def test_manual_dry_run_mutates_nothing(fake_service):
    payload = await alpaca_paper_submit_order(**_BUY, confirm=False)
    assert payload["submitted"] is False
    assert payload["blocked_reason"] == "confirmation_required"
    assert fake_service.calls == []
    assert await _count_rows("rob73-") == 0  # no ledger row written on dry-run


# ---------------------------------------------------------------------------
# F1 — manual confirm requires server-observed market evidence (no origin bypass)
# ---------------------------------------------------------------------------
async def test_manual_confirm_without_snapshot_fails_close(fake_service):
    payload = await alpaca_paper_submit_order(**_BUY, confirm=True)
    assert payload["success"] is False
    assert payload["reason_code"] == "missing_market_evidence"
    assert fake_service.calls == []
    assert await _count_rows("rob73-") == 0


async def test_manual_confirm_with_stale_snapshot_fails_close(fake_service):
    from datetime import UTC, datetime, timedelta

    from app.models.market_quote_snapshot import MarketQuoteSnapshot

    async with AsyncSessionLocal() as db:
        row = MarketQuoteSnapshot(
            market="us",
            symbol="AAPL",
            source="yahoo",
            snapshot_at=datetime.now(UTC) - timedelta(hours=1),
            price=Decimal("150"),
        )
        db.add(row)
        await db.commit()
        sid = row.id
    payload = await alpaca_paper_submit_order(
        **_BUY, quote_snapshot_id=sid, confirm=True
    )
    assert payload["success"] is False
    assert payload["reason_code"] == "stale_trusted_snapshot"
    assert fake_service.calls == []


# ---------------------------------------------------------------------------
# F3 — public success contract for manual (422 => success=false)
# ---------------------------------------------------------------------------
async def test_manual_http_422_success_false(monkeypatch):
    from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError

    class _Raising(FakeOrdersService):
        async def submit_order(self, request):  # type: ignore[override]
            self.calls.append(("submit_order", {"request": request}))
            raise AlpacaPaperRequestError("bad", status_code=422)

    service = _Raising()
    set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
    try:
        sid = await _seed_snapshot()
        payload = await alpaca_paper_submit_order(
            **_BUY, quote_snapshot_id=sid, confirm=True
        )
        assert payload["status"] == "failed"
        assert payload["success"] is False
        assert len([c for c in service.calls if c[0] == "submit_order"]) == 1
    finally:
        reset_alpaca_paper_orders_service_factory()


# ---------------------------------------------------------------------------
# G2 — manual equity market qty is bound by trusted reference_price × qty
# ---------------------------------------------------------------------------
async def test_manual_market_qty_bypass_of_hard_cap_fails_close(fake_service):
    # AAPL market qty=5 at trusted price 100,000 => $500,000 implied notional.
    sid = await _seed_snapshot(price="100000")
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="market",
        qty=Decimal("5"),
        quote_snapshot_id=sid,
        confirm=True,
    )
    assert payload["success"] is False
    assert payload["reason_code"] == "notional_exceeds_max"
    assert [c for c in fake_service.calls if c[0] == "submit_order"] == []


async def test_manual_notional_within_cap_still_posts(fake_service):
    # Regression: a normal small order still goes through.
    sid = await _seed_snapshot(price="150")
    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="buy",
        type="market",
        notional=Decimal("300"),
        quote_snapshot_id=sid,
        confirm=True,
    )
    assert payload["submitted"] is True
    assert len([c for c in fake_service.calls if c[0] == "submit_order"]) == 1


@pytest.mark.parametrize(
    "qty_available, reason_code",
    [
        (None, "position_available_unavailable"),
        (Decimal("NaN"), "position_available_malformed"),
        (Decimal("Infinity"), "position_available_malformed"),
        (Decimal("-1"), "position_available_malformed"),
    ],
)
async def test_manual_sell_invalid_qty_available_fails_closed(
    fake_service, qty_available, reason_code
):
    async def _get_position(_symbol):
        return SimpleNamespace(
            symbol="AAPL", qty=Decimal("1"), qty_available=qty_available
        )

    fake_service.get_position = _get_position  # type: ignore[assignment]
    sid = await _seed_snapshot(price="100")

    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="sell",
        type="limit",
        qty=Decimal("0.4"),
        limit_price=Decimal("100"),
        quote_snapshot_id=sid,
        confirm=True,
    )

    assert payload["status"] == "rejected"
    assert payload["reason_code"] == reason_code
    assert [c for c in fake_service.calls if c[0] == "submit_order"] == []


async def test_manual_sell_uses_broker_qty_available_as_hard_upper_bound(fake_service):
    async def _get_position(_symbol):
        return SimpleNamespace(
            symbol="AAPL", qty=Decimal("1"), qty_available=Decimal("0.4")
        )

    fake_service.get_position = _get_position  # type: ignore[assignment]
    sid = await _seed_snapshot(price="100")

    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="sell",
        type="limit",
        qty=Decimal("0.7"),
        limit_price=Decimal("100"),
        quote_snapshot_id=sid,
        confirm=True,
    )

    assert payload["status"] == "rejected"
    assert payload["reason_code"] == "qty_exceeds_available"
    assert [c for c in fake_service.calls if c[0] == "submit_order"] == []


async def test_manual_sell_claim_persists_position_baseline(fake_service):
    async def _get_position(_symbol):
        return SimpleNamespace(
            symbol="AAPL", qty=Decimal("1"), qty_available=Decimal("1")
        )

    fake_service.get_position = _get_position  # type: ignore[assignment]
    sid = await _seed_snapshot(price="100")

    payload = await alpaca_paper_submit_order(
        symbol="AAPL",
        side="sell",
        type="limit",
        qty=Decimal("0.4"),
        limit_price=Decimal("100"),
        quote_snapshot_id=sid,
        confirm=True,
    )

    assert payload["submitted"] is True
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AlpacaPaperOrderLedger).where(
                    AlpacaPaperOrderLedger.client_order_id
                    == payload["client_order_id"],
                    AlpacaPaperOrderLedger.record_kind == "execution",
                )
            )
        ).scalar_one()
    assert row.position_snapshot["snapshot_kind"] == "sell_claim_baseline"
    assert row.position_snapshot["qty"] == "1"
    assert row.position_snapshot["qty_available"] == "1"
    assert row.position_snapshot["fetched_at"]


# ---------------------------------------------------------------------------
# G4c — the cancel tool releases a sell reservation via the ledger
# ---------------------------------------------------------------------------
async def test_cancel_tool_releases_sell_reservation(fake_service):
    from datetime import UTC, datetime

    from app.mcp_server.tooling.alpaca_paper_orders import alpaca_paper_cancel_order
    from app.models.trading import InstrumentType
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
    from app.services.brokers.alpaca.schemas import Order

    coid = "rob74-crypto-cxlreserve"
    # Seed an OPEN (submitted) sell reservation row.
    async with AsyncSessionLocal() as db:
        db.add(
            AlpacaPaperOrderLedger(
                client_order_id=coid,
                lifecycle_correlation_id=coid,
                record_kind="execution",
                broker="alpaca",
                account_mode="alpaca_paper",
                lifecycle_state="submitted",
                execution_symbol="BTC/USD",
                execution_venue="alpaca_paper",
                instrument_type=InstrumentType.crypto,
                side="sell",
                order_type="limit",
                currency="USD",
                requested_qty=Decimal("0.5"),
                submitted_at=datetime.now(UTC),
                broker_order_id="paper-cxl-1",
                confirm_flag=True,
            )
        )
        await db.commit()

    # The cancel read-back returns the order carrying the client_order_id.
    async def _get_order(_id):
        return Order(
            id="paper-cxl-1",
            client_order_id=coid,
            symbol="BTC/USD",
            filled_qty=Decimal("0"),
            side="sell",
            type="limit",
            time_in_force="gtc",
            status="canceled",
        )

    fake_service.get_order = _get_order  # type: ignore[assignment]
    result = await alpaca_paper_cancel_order(order_id="paper-cxl-1", confirm=True)
    assert result["reservation_released"] is True

    # The row is now canceled -> no longer counts against sellable position.
    from app.services.alpaca_paper_ledger_service import (
        AlpacaPaperLedgerService as _L,
    )

    async with AsyncSessionLocal() as db:
        claim = await _L(db).reserve_sell_and_claim(
            client_order_id="rob74-crypto-afterc",
            lifecycle_correlation_id="rob74-crypto-afterc",
            execution_symbol="BTC/USD",
            execution_venue="alpaca_paper",
            instrument_type=InstrumentType.crypto,
            requested_qty=Decimal("1"),
            position_qty=Decimal("1"),
            position_available=Decimal("1"),
        )
        assert claim.insufficient is False  # reservation released -> full position free
        _ = AlpacaPaperLedgerService  # keep import used


# ---------------------------------------------------------------------------
# H1 — cancel releases the reservation ONLY on a confirmed terminal `canceled`
# ---------------------------------------------------------------------------
async def _seed_open_sell(coid: str, qty: str = "0.5") -> None:
    from datetime import UTC, datetime

    from app.models.trading import InstrumentType

    async with AsyncSessionLocal() as db:
        db.add(
            AlpacaPaperOrderLedger(
                client_order_id=coid,
                lifecycle_correlation_id=coid,
                record_kind="execution",
                broker="alpaca",
                account_mode="alpaca_paper",
                lifecycle_state="submitted",
                execution_symbol="BTC/USD",
                execution_venue="alpaca_paper",
                instrument_type=InstrumentType.crypto,
                side="sell",
                order_type="limit",
                currency="USD",
                requested_qty=Decimal(qty),
                submitted_at=datetime.now(UTC),
                broker_order_id=f"b-{coid}",
                confirm_flag=True,
            )
        )
        await db.commit()


async def _open_reserved_qty() -> Decimal:
    from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

    async with AsyncSessionLocal() as db:
        rows = await AlpacaPaperLedgerService(db).list_open_sells(
            account_mode="alpaca_paper", execution_symbol="BTC/USD"
        )
        return sum((Decimal(str(r.requested_qty)) for r in rows), Decimal("0"))


async def _ledger_row(coid: str) -> AlpacaPaperOrderLedger:
    async with AsyncSessionLocal() as db:
        return (
            await db.execute(
                select(AlpacaPaperOrderLedger).where(
                    AlpacaPaperOrderLedger.client_order_id == coid
                )
            )
        ).scalar_one()


def _cancel_readback(coid: str, status: str):
    from app.services.brokers.alpaca.schemas import Order

    async def _get_order(_id):
        return Order(
            id="b-cxl",
            client_order_id=coid,
            symbol="BTC/USD",
            filled_qty=Decimal("0"),
            side="sell",
            type="limit",
            time_in_force="gtc",
            status=status,
        )

    return _get_order


async def test_cancel_pending_cancel_keeps_reservation(fake_service):
    coid = "rob74-crypto-pendcxl"
    await _seed_open_sell(coid, qty="0.6")
    fake_service.get_order = _cancel_readback(coid, "pending_cancel")  # type: ignore[assignment]

    result = await alpaca_paper_cancel_order(order_id="b-cxl", confirm=True)

    assert result["cancel_requested"] is True
    assert result["cancelled"] is False  # NOT terminal-canceled
    assert result["order_status"] == "pending_cancel"
    assert result["reservation_released"] is False
    assert await _open_reserved_qty() == Decimal("0.6")  # still reserved


async def test_cancel_confirmed_canceled_releases_reservation(fake_service):
    coid = "rob74-crypto-realcxl"
    await _seed_open_sell(coid, qty="0.6")
    fake_service.get_order = _cancel_readback(coid, "canceled")  # type: ignore[assignment]

    result = await alpaca_paper_cancel_order(order_id="b-cxl", confirm=True)

    assert result["cancelled"] is True
    assert result["reservation_released"] is True
    assert await _open_reserved_qty() == Decimal("0")  # released


async def test_cancel_racing_fill_converges_to_filled_not_canceled(fake_service):
    coid = "rob74-crypto-racefill"
    await _seed_open_sell(coid, qty="0.6")
    fake_service.get_order = _cancel_readback(coid, "filled")  # type: ignore[assignment]

    result = await alpaca_paper_cancel_order(order_id="b-cxl", confirm=True)

    # A cancel that raced a fill converges to filled, not canceled.
    assert result["cancelled"] is False
    assert result["order_status"] == "filled"
    assert result["reservation_released"] is False
    assert result["lifecycle_synced"] is True
    # Fill truth is returned, but the hold remains until position evidence proves
    # the fill is reflected (the cancel read-back has no causal position snapshot).
    assert await _open_reserved_qty() == Decimal("0.6")

    row = await _ledger_row(coid)
    assert row.order_status == "filled"
    assert row.filled_qty == Decimal("0")
    assert row.lifecycle_state == "submitted"


@pytest.mark.parametrize(
    ("broker_status", "filled_qty"),
    [
        ("accepted", "0"),
        ("partially_filled", "0.2"),
        ("filled", "0.6"),
    ],
)
async def test_cancel_known_hold_status_persists_truth_without_release(
    fake_service, broker_status, filled_qty
):
    """Known open/partial/filled truth is synced while lifecycle stays held."""
    coid = f"rob74-crypto-cancel-truth-{broker_status}"
    await _seed_open_sell(coid, qty="0.6")

    async def _get_order(_id):
        from app.services.brokers.alpaca.schemas import Order

        return Order(
            id="b-cxl",
            client_order_id=coid,
            symbol="BTC/USD",
            filled_qty=Decimal(filled_qty),
            side="sell",
            type="limit",
            time_in_force="gtc",
            status=broker_status,
        )

    fake_service.get_order = _get_order  # type: ignore[assignment]

    result = await alpaca_paper_cancel_order(order_id="b-cxl", confirm=True)
    row = await _ledger_row(coid)

    assert result["cancelled"] is False
    assert result["reservation_released"] is False
    assert result["lifecycle_synced"] is True
    assert row.order_status == broker_status
    assert row.filled_qty == Decimal(filled_qty)
    assert row.lifecycle_state == "submitted"
    assert await _open_reserved_qty() == Decimal("0.6")


@pytest.mark.parametrize("unknown_status", ["pending_review", " ", 123])
async def test_cancel_unknown_status_keeps_db_reservation(fake_service, unknown_status):
    """An unrecognised broker status must not silently release a sell hold."""
    coid = "rob74-crypto-unknowncxl"
    await _seed_open_sell(coid, qty="0.6")

    async def _get_order(_id):
        return {
            "id": "b-cxl",
            "client_order_id": coid,
            "symbol": "BTC/USD",
            "filled_qty": "0",
            "side": "sell",
            "type": "limit",
            "time_in_force": "gtc",
            "status": unknown_status,
        }

    fake_service.get_order = _get_order  # type: ignore[assignment]

    result = await alpaca_paper_cancel_order(order_id="b-cxl", confirm=True)

    assert result["cancelled"] is False
    assert result["reservation_released"] is False
    assert result["lifecycle_synced"] is False
    assert await _open_reserved_qty() == Decimal("0.6")
