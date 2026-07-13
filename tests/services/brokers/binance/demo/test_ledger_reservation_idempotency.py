"""ROB-845 deterministic Binance Demo root reservation contracts.

The deterministic paper-execution identity is resolved under the same
PostgreSQL advisory lock as the ROB-844 exposure reservation.  Replays are
classified before exposure-cap checks and never insert a second native row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_NOW = dt.datetime(2026, 7, 13, 1, 0, tzinfo=dt.UTC)
_PREFIX = "rob845r-"
_CID = f"{_PREFIX}same-intent"
_IDENTITY = {
    "decision_id": "decision-1",
    "intent_hash": "a" * 64,
    "close_client_order_id": "rob845c-same-intent",
}


async def _instrument(session, symbol: str) -> int:
    existing = await session.scalar(
        select(CryptoInstrument.id).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "spot",
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    if existing is not None:
        return existing
    instrument = CryptoInstrument(
        venue="binance",
        product="spot",
        venue_symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        quote_asset="USDT",
        status="active",
    )
    session.add(instrument)
    await session.flush()
    await session.refresh(instrument)
    await session.commit()
    return instrument.id


async def _reserve(
    session,
    *,
    instrument_id: int,
    identity: dict[str, str] | None = _IDENTITY,
    cap: int = 100,
):
    return await BinanceDemoLedgerService(session).reserve_root_planned(
        instrument_id=instrument_id,
        product="spot",
        venue_host="demo-api.binance.com",
        client_order_id=_CID,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("0.001"),
        price=None,
        notional_usdt=Decimal("10"),
        extra_metadata={"paper_execution_identity": identity},
        idempotency_metadata=identity,
        global_open_root_cap=cap,
        now=_NOW,
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(binance_demo_reservation_lock):
    async def clean() -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id.like(f"{_PREFIX}%")
                )
            )
            await session.commit()

    await clean()
    yield
    await clean()


@pytest.mark.asyncio
async def test_same_inflight_identity_returns_in_progress_before_cap() -> None:
    async with AsyncSessionLocal() as session:
        instrument_id = await _instrument(session, "ROB845AUSDT")
        first = await _reserve(session, instrument_id=instrument_id)
        second = await _reserve(session, instrument_id=instrument_id, cap=0)

    assert first.status == "reserved"
    assert second.status == "idempotency_in_progress"
    assert second.row is not None
    assert second.row.client_order_id == _CID


@pytest.mark.asyncio
async def test_same_terminal_identity_returns_replayed_before_cap() -> None:
    async with AsyncSessionLocal() as session:
        instrument_id = await _instrument(session, "ROB845BUSDT")
        await _reserve(session, instrument_id=instrument_id)
        ledger = BinanceDemoLedgerService(session)
        await ledger.record_previewed(client_order_id=_CID, now=_NOW)
        await ledger.record_validated(client_order_id=_CID, now=_NOW)
        await ledger.record_submitted(
            client_order_id=_CID,
            broker_order_id="broker-rob845-terminal",
            now=_NOW,
        )
        await ledger.record_filled(client_order_id=_CID, now=_NOW)
        await ledger.record_closed(client_order_id=_CID, now=_NOW)
        await ledger.record_reconciled(client_order_id=_CID, now=_NOW)
        await session.commit()

        replay = await _reserve(session, instrument_id=instrument_id, cap=0)

    assert replay.status == "replayed"
    assert replay.row is not None
    assert replay.row.lifecycle_state == "reconciled"


@pytest.mark.asyncio
async def test_same_client_id_with_different_identity_returns_collision() -> None:
    async with AsyncSessionLocal() as session:
        instrument_id = await _instrument(session, "ROB845CUSDT")
        await _reserve(session, instrument_id=instrument_id)
        collision = await _reserve(
            session,
            instrument_id=instrument_id,
            identity={**_IDENTITY, "intent_hash": "b" * 64},
        )

    assert collision.status == "idempotency_collision"
    assert collision.row is not None
    assert collision.reason == "immutable_metadata_mismatch"


@pytest.mark.asyncio
async def test_concurrent_same_identity_inserts_exactly_one_root() -> None:
    async with AsyncSessionLocal() as setup:
        instrument_id = await _instrument(setup, "ROB845DUSDT")

    barrier = asyncio.Barrier(2)

    async def reserve_once():
        async with AsyncSessionLocal() as session:
            await barrier.wait()
            return await _reserve(session, instrument_id=instrument_id)

    first, second = await asyncio.gather(reserve_once(), reserve_once())

    assert {first.status, second.status} == {
        "reserved",
        "idempotency_in_progress",
    }
    async with AsyncSessionLocal() as verify:
        count = await verify.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(BinanceDemoOrderLedger.client_order_id == _CID)
        )
    assert count == 1
