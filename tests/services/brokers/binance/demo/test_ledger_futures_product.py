"""ROB-298 PR 2 — Demo ledger correctly handles ``product='usdm_futures'`` rows.

The ``BinanceDemoLedgerService`` and ``BinanceDemoLedgerRepository`` are
product-agnostic. PR 1 introduced the table with a CHECK constraint
``product IN ('spot','usdm_futures')``. This file verifies futures rows
work identically to spot rows.

Service surface (write methods): no behavior changes between products.
The futures-specific business logic (leverage / position mode / reduceOnly)
lives in the execution client, not the ledger.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

# The real futures smoke commits an XRPUSDT root on a separate xdist worker.
# Hold the shared test-only lock so this module's deliberately uncommitted raw
# ledger roots cannot overlap that committed row under the production partial
# unique index.  The smoke fixture removes its committed residue before it
# releases the same lock.
pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")


@pytest_asyncio.fixture
async def demo_ledger_service(db_session) -> BinanceDemoLedgerService:
    """Reuse the fixture pattern from test_ledger_service.py."""
    return BinanceDemoLedgerService(db_session)


@pytest_asyncio.fixture
async def crypto_instrument_xrp_id(db_session) -> int:
    """Find-or-create XRPUSDT futures instrument row.

    Mirrors the BTC fixture in test_ledger_service.py but uses XRPUSDT
    + product='usdm_futures' + venue='binance'.

    The shared ``db_session`` fixture does not roll back between tests, so the
    unique ``(venue, product, venue_symbol)`` row may already exist.
    """
    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "usdm_futures",
            CryptoInstrument.venue_symbol == "XRPUSDT",
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol="XRPUSDT",
        base_asset="XRP",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst.id


@pytest.mark.asyncio
async def test_record_planned_usdm_futures(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify record_planned works identically with product='usdm_futures'."""
    now = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-planned"
    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("16.6"),
        price=None,
        now=now,
    )
    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.product == "usdm_futures"
    assert row.venue_host == "demo-fapi.binance.com"
    assert row.lifecycle_state == "planned"
    assert row.side == "BUY"
    assert row.qty == Decimal("16.6")


@pytest.mark.asyncio
async def test_futures_state_transitions_full_lifecycle(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify full lifecycle: planned → previewed → validated → submitted →
    filled → closed → reconciled.

    This tests the canonical happy path for futures orders, proving that
    state transitions work identically to spot orders.
    """
    base = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-lifecycle"

    # planned
    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("16.6"),
        price=None,
        now=base,
    )

    # planned → previewed
    await demo_ledger_service.record_previewed(
        client_order_id=cid, now=base + dt.timedelta(seconds=1)
    )

    # previewed → validated
    await demo_ledger_service.record_validated(
        client_order_id=cid, now=base + dt.timedelta(seconds=2)
    )

    # validated → submitted
    await demo_ledger_service.record_submitted(
        client_order_id=cid,
        broker_order_id="42",
        now=base + dt.timedelta(seconds=3),
    )

    # submitted → filled
    await demo_ledger_service.record_filled(
        client_order_id=cid, now=base + dt.timedelta(seconds=4)
    )

    # filled → closed
    await demo_ledger_service.record_closed(
        client_order_id=cid, now=base + dt.timedelta(seconds=5)
    )

    # closed → reconciled
    await demo_ledger_service.record_reconciled(
        client_order_id=cid, now=base + dt.timedelta(seconds=6)
    )

    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.lifecycle_state == "reconciled"
    assert row.broker_order_id == "42"
    assert row.previewed_at == base + dt.timedelta(seconds=1)
    assert row.validated_at == base + dt.timedelta(seconds=2)
    assert row.submitted_at == base + dt.timedelta(seconds=3)
    assert row.filled_at == base + dt.timedelta(seconds=4)
    assert row.closed_at == base + dt.timedelta(seconds=5)
    assert row.reconciled_at == base + dt.timedelta(seconds=6)
    assert row.last_reconciled_at == base + dt.timedelta(seconds=6)


@pytest.mark.asyncio
async def test_futures_cancelled_path(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify cancellation path: planned → cancelled → reconciled.

    Confirms that the cancellation branch works for futures orders.
    """
    base = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-cancelled"

    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("16.6"),
        price=None,
        now=base,
    )

    await demo_ledger_service.record_cancelled(
        client_order_id=cid, now=base + dt.timedelta(seconds=1)
    )

    await demo_ledger_service.record_reconciled(
        client_order_id=cid, now=base + dt.timedelta(seconds=2)
    )

    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.lifecycle_state == "reconciled"
    assert row.cancelled_at == base + dt.timedelta(seconds=1)


@pytest.mark.asyncio
async def test_futures_anomaly_path(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify anomaly path: planned → anomaly (terminal).

    Confirms that anomaly transitions work for futures orders.
    """
    base = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-anomaly"

    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("16.6"),
        price=None,
        now=base,
    )

    await demo_ledger_service.record_anomaly(
        client_order_id=cid,
        reason="stale position after close",
        now=base + dt.timedelta(seconds=1),
    )

    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.lifecycle_state == "anomaly"
    assert row.anomaly_reason == "stale position after close"


@pytest.mark.asyncio
async def test_futures_short_order(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify futures short orders work (side='SELL').

    Futures-specific: spot does not support short-selling natively, but
    futures do. Confirm the service handles SELL side identically.
    """
    now = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-short"

    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="SELL",
        order_type="MARKET",
        qty=Decimal("25.0"),
        price=None,
        now=now,
    )

    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.side == "SELL"
    assert row.qty == Decimal("25.0")
    assert row.product == "usdm_futures"
    assert row.lifecycle_state == "planned"


@pytest.mark.asyncio
async def test_futures_limit_order(
    demo_ledger_service: BinanceDemoLedgerService,
    crypto_instrument_xrp_id: int,
) -> None:
    """Verify futures limit orders work (with price).

    Confirms that limit orders (vs. market orders) are supported identically
    for futures.
    """
    now = dt.datetime(2026, 5, 23, 12, 0, 0, tzinfo=dt.UTC)
    cid = "test-futures-XRPUSDT-limit"

    await demo_ledger_service.record_planned(
        instrument_id=crypto_instrument_xrp_id,
        product="usdm_futures",
        venue_host="demo-fapi.binance.com",
        client_order_id=cid,
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("50.0"),
        price=Decimal("2.25"),
        now=now,
    )

    row = await demo_ledger_service.get_by_client_order_id(cid)
    assert row is not None
    assert row.order_type == "LIMIT"
    assert row.price == Decimal("2.25")
    assert row.qty == Decimal("50.0")
