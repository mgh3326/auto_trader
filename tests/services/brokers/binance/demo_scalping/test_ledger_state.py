"""ROB-307 PR1 — tests for ledger-backed durable state (§4).

Cooldown and "one open lifecycle per product+symbol" are read from
``binance_demo_order_ledger`` via the sanctioned ledger service, never
from in-memory state. The fresh-service-instance test proves enforcement
survives a new process / scheduler run: a brand-new service object reads
the same committed rows.

The shared ``db_session`` fixture is not rolled back between tests, so
these tests use unique symbols and delta-based assertions for table-wide
counts.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.ledger_state import (
    load_ledger_snapshot,
)

# ROB-844: some assertions read the table-wide open-root / orders-today counts.
# Serialize against the open-root-committing executor family (which now commits
# planned roots via reserve_root_planned) so those table-wide deltas are stable
# under --dist=loadfile (ROB-842).
pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def service(db_session) -> BinanceDemoLedgerService:
    return BinanceDemoLedgerService(db_session)


async def _instrument_id(db_session, symbol: str, product: str = "spot") -> int:
    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == product,
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product=product,
        venue_symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst.id


async def _plan(
    service: BinanceDemoLedgerService,
    *,
    instrument_id: int,
    coid: str,
    now: dt.datetime = _NOW,
    product: str = "spot",
) -> None:
    await service.record_planned(
        instrument_id=instrument_id,
        product=product,
        venue_host="demo-api.binance.com",
        client_order_id=coid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("1"),
        price=None,
        now=now,
    )


async def _drive_to_closed(
    service: BinanceDemoLedgerService,
    *,
    coid: str,
    closed_at: dt.datetime,
    realized_pnl_usdt: str | None = None,
) -> None:
    await service.record_previewed(client_order_id=coid, now=closed_at)
    await service.record_validated(client_order_id=coid, now=closed_at)
    await service.record_submitted(
        client_order_id=coid, broker_order_id=f"b-{coid}", now=closed_at
    )
    await service.record_filled(client_order_id=coid, now=closed_at)
    merge = (
        {"realized_pnl_usdt": realized_pnl_usdt}
        if realized_pnl_usdt is not None
        else None
    )
    await service.record_closed(
        client_order_id=coid, now=closed_at, extra_metadata_merge=merge
    )


@pytest.mark.asyncio
async def test_fresh_symbol_has_no_open_lifecycle(service, db_session) -> None:
    snap = await load_ledger_snapshot(
        service, product="spot", symbol="ZZZUSDT", now=_NOW
    )
    assert snap.has_open_lifecycle_for_symbol is False
    assert snap.seconds_since_last_close_for_symbol is None


@pytest.mark.asyncio
async def test_open_planned_row_marks_symbol_open(service, db_session) -> None:
    iid = await _instrument_id(db_session, "AAAUSDT")
    await _plan(service, instrument_id=iid, coid="ls-open-aaa")
    snap = await load_ledger_snapshot(
        service, product="spot", symbol="AAAUSDT", now=_NOW
    )
    assert snap.has_open_lifecycle_for_symbol is True


@pytest.mark.asyncio
async def test_closed_row_frees_symbol_and_sets_cooldown(service, db_session) -> None:
    iid = await _instrument_id(db_session, "BBBUSDT")
    closed_at = _NOW - dt.timedelta(seconds=120)
    await _plan(service, instrument_id=iid, coid="ls-closed-bbb", now=closed_at)
    await _drive_to_closed(service, coid="ls-closed-bbb", closed_at=closed_at)
    snap = await load_ledger_snapshot(
        service, product="spot", symbol="BBBUSDT", now=_NOW
    )
    assert snap.has_open_lifecycle_for_symbol is False
    assert snap.seconds_since_last_close_for_symbol == pytest.approx(120.0, abs=1.0)


@pytest.mark.asyncio
async def test_realized_loss_today_sums_negative_pnl(service, db_session) -> None:
    before = (
        await load_ledger_snapshot(service, product="spot", symbol="CCCUSDT", now=_NOW)
    ).realized_loss_today_usdt
    iid = await _instrument_id(db_session, "CCCUSDT")
    await _plan(service, instrument_id=iid, coid="ls-loss-ccc")
    await _drive_to_closed(
        service, coid="ls-loss-ccc", closed_at=_NOW, realized_pnl_usdt="-2.5"
    )
    after = (
        await load_ledger_snapshot(service, product="spot", symbol="CCCUSDT", now=_NOW)
    ).realized_loss_today_usdt
    assert after - before == Decimal("2.5")


@pytest.mark.asyncio
async def test_orders_today_counts_lifecycles_since_midnight(
    service, db_session
) -> None:
    iid = await _instrument_id(db_session, "DDDUSDT")
    before = (
        await load_ledger_snapshot(service, product="spot", symbol="DDDUSDT", now=_NOW)
    ).orders_today
    # Two *sequential* lifecycles for one symbol (open → close → re-open): the
    # first must reach a terminal state before the second opens, which the
    # ROB-844 one-open-root invariant now enforces. Both still count toward
    # orders_today (planned_at since midnight).
    await _plan(service, instrument_id=iid, coid="ls-cnt-ddd-1")
    await _drive_to_closed(service, coid="ls-cnt-ddd-1", closed_at=_NOW)
    await _plan(service, instrument_id=iid, coid="ls-cnt-ddd-2")
    after = (
        await load_ledger_snapshot(service, product="spot", symbol="DDDUSDT", now=_NOW)
    ).orders_today
    assert after - before == 2


@pytest.mark.asyncio
async def test_fresh_service_instance_reads_committed_state(
    service, db_session
) -> None:
    # §4: durable across a new process/run — a brand-new service object,
    # holding no in-memory state, sees the same rows.
    iid = await _instrument_id(db_session, "EEEUSDT")
    await _plan(service, instrument_id=iid, coid="ls-fresh-eee")
    fresh_service = BinanceDemoLedgerService(db_session)
    snap = await load_ledger_snapshot(
        fresh_service, product="spot", symbol="EEEUSDT", now=_NOW
    )
    assert snap.has_open_lifecycle_for_symbol is True
