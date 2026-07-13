"""ROB-844 — atomic root-entry reservation + broker-ack uniqueness.

The reservation closes the count→insert TOCTOU that let two processes
(TaskIQ / MCP / websocket) both pass the open-lifecycle check and each submit a
Binance Demo root order. ``reserve_root_planned`` re-checks the global open-root
cap and the per-instrument open root under a transaction-scoped advisory lock
and inserts the planned root in the same transaction, committing so the claim is
durable; only the winner proceeds to the broker.

These tests run against real PostgreSQL. Concurrency tests use two independent
committed sessions with an ``asyncio.Barrier`` to exercise the cross-process
race. The whole file holds ``binance_demo_reservation_lock`` so the table-wide
global open-root count is stable against other open-root committers under
``--dist=loadfile`` (ROB-842).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.errors import (
    BinanceDemoDuplicateAcknowledgement,
)
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo.ledger.repository import (
    BinanceDemoLedgerRepository,
)

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_HOST = "demo-api.binance.com"
_CID_PREFIX = "rob844-"
_HIGH_CAP = 1_000_000  # per-instrument tests: never trip the global cap


async def _instrument(session, symbol: str, product: str = "spot") -> int:
    existing = await session.scalar(
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
    session.add(inst)
    await session.flush()
    await session.refresh(inst)
    iid = inst.id
    await session.commit()
    return iid


async def _reserve(session, *, instrument_id, cid, cap=_HIGH_CAP, product="spot"):
    return await BinanceDemoLedgerService(session).reserve_root_planned(
        instrument_id=instrument_id,
        product=product,
        venue_host=_HOST,
        client_order_id=cid,
        side="BUY",
        order_type="MARKET",
        qty=Decimal("1"),
        price=None,
        notional_usdt=Decimal("10"),
        global_open_root_cap=cap,
        now=_NOW,
    )


@pytest_asyncio.fixture(autouse=True)
async def _clean_test_rows():
    async def _c():
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id.like(f"{_CID_PREFIX}%")
                )
            )
            await db.commit()

    await _c()
    yield
    await _c()


# --------------------------------------------------------------------------- #
# Single-session reservation semantics
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reserve_inserts_planned_root() -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844AUSDT")
        result = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}a1")
    assert result.status == "reserved"
    assert result.row is not None
    assert result.row.lifecycle_state == "planned"
    assert result.row.parent_client_order_id is None


@pytest.mark.asyncio
async def test_second_reservation_same_instrument_slot_taken() -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844BUSDT")
        first = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}b1")
        second = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}b2")
    assert first.status == "reserved"
    assert second.status == "exposure_slot_taken"
    assert second.reason == "instrument_open_root"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["closed", "cancelled", "reconciled"])
async def test_terminal_state_releases_slot(terminal: str) -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844CUSDT")
        svc = BinanceDemoLedgerService(db)
        cid = f"{_CID_PREFIX}c-{terminal}"
        await _reserve(db, instrument_id=iid, cid=cid)
        # Drive the root to the terminal state under test.
        await svc.record_previewed(client_order_id=cid, now=_NOW)
        await svc.record_validated(client_order_id=cid, now=_NOW)
        if terminal == "cancelled":
            await svc.record_cancelled(client_order_id=cid, now=_NOW)
        else:
            await svc.record_submitted(
                client_order_id=cid, broker_order_id=f"bk-{cid}", now=_NOW
            )
            await svc.record_filled(client_order_id=cid, now=_NOW)
            await svc.record_closed(client_order_id=cid, now=_NOW)
            if terminal == "reconciled":
                await svc.record_reconciled(client_order_id=cid, now=_NOW)
        await db.commit()
        # A terminal root frees the slot: re-reservation is admitted.
        again = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}c-reentry")
    assert again.status == "reserved"


@pytest.mark.asyncio
async def test_anomaly_state_blocks_reservation() -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844DUSDT")
        svc = BinanceDemoLedgerService(db)
        cid = f"{_CID_PREFIX}d1"
        await _reserve(db, instrument_id=iid, cid=cid)
        await svc.record_anomaly(client_order_id=cid, reason="stuck", now=_NOW)
        await db.commit()
        blocked = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}d2")
    assert blocked.status == "exposure_slot_taken"
    assert blocked.reason == "instrument_open_root"


@pytest.mark.asyncio
async def test_close_child_not_blocked_and_not_counted() -> None:
    """A close/reduce-only child (parent set) never consumes a root slot."""
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844EUSDT")
        svc = BinanceDemoLedgerService(db)
        repo = BinanceDemoLedgerRepository(db)
        root_cid = f"{_CID_PREFIX}e-root"
        await _reserve(db, instrument_id=iid, cid=root_cid)
        before = await repo.count_open_root_lifecycles()
        # Insert a child close leg for the SAME instrument while the root is open.
        await svc.record_planned(
            instrument_id=iid,
            product="spot",
            venue_host=_HOST,
            client_order_id=f"{_CID_PREFIX}e-close",
            side="SELL",
            order_type="MARKET",
            qty=Decimal("1"),
            price=None,
            parent_client_order_id=root_cid,
            now=_NOW,
        )
        await db.commit()
        after = await repo.count_open_root_lifecycles()
        has_root = await repo.has_open_root_lifecycle_for_instrument(
            product="spot", instrument_id=iid
        )
    assert before == after  # the child did not add to the root count
    assert has_root is True  # only the root occupies the slot


@pytest.mark.asyncio
async def test_global_cap_zero_blocks() -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844FUSDT")
        result = await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}f1", cap=0)
    assert result.status == "exposure_slot_taken"
    assert result.reason == "global_open_root_cap"


@pytest.mark.asyncio
async def test_duplicate_broker_ack_normalized_no_integrityerror() -> None:
    """A replayed broker ack on a second row → typed duplicate, not IntegrityError."""
    async with AsyncSessionLocal() as db:
        svc = BinanceDemoLedgerService(db)
        iid_a = await _instrument(db, "R844GUSDT")
        iid_b = await _instrument(db, "R844HUSDT")
        cid_a = f"{_CID_PREFIX}g-a"
        cid_b = f"{_CID_PREFIX}g-b"
        # Row A reaches submitted with a broker ack.
        await _reserve(db, instrument_id=iid_a, cid=cid_a)
        await svc.record_previewed(client_order_id=cid_a, now=_NOW)
        await svc.record_validated(client_order_id=cid_a, now=_NOW)
        await svc.record_submitted(
            client_order_id=cid_a, broker_order_id="SHARED-ACK", now=_NOW
        )
        await db.commit()
        # Row B (different instrument) tries to attach the SAME broker ack.
        await _reserve(db, instrument_id=iid_b, cid=cid_b)
        await svc.record_previewed(client_order_id=cid_b, now=_NOW)
        await svc.record_validated(client_order_id=cid_b, now=_NOW)
        with pytest.raises(BinanceDemoDuplicateAcknowledgement) as exc_info:
            await svc.record_submitted(
                client_order_id=cid_b, broker_order_id="SHARED-ACK", now=_NOW
            )
        assert exc_info.value.result == "duplicate_acknowledgement"
        await db.rollback()
        # The first row still owns the ack; no second row captured it.
        async with AsyncSessionLocal() as verify:
            owners = await verify.scalar(
                select(func.count())
                .select_from(BinanceDemoOrderLedger)
                .where(BinanceDemoOrderLedger.broker_order_id == "SHARED-ACK")
            )
    assert owners == 1


@pytest.mark.asyncio
async def test_abandoned_planned_reservation_not_auto_released() -> None:
    """A durable planned reservation keeps blocking — no timeout/TTL release."""
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844IUSDT")
        await _reserve(db, instrument_id=iid, cid=f"{_CID_PREFIX}i1")
        # A much-later reservation attempt still fails closed: the abandoned
        # planned row is a durable in-flight marker, released only by an explicit
        # reconciler proving broker truth (ROB-844 §6) — never by elapsed time.
        much_later = BinanceDemoLedgerService(db)
        result = await much_later.reserve_root_planned(
            instrument_id=iid,
            product="spot",
            venue_host=_HOST,
            client_order_id=f"{_CID_PREFIX}i2",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("1"),
            price=None,
            notional_usdt=Decimal("10"),
            global_open_root_cap=_HIGH_CAP,
            now=_NOW + dt.timedelta(days=3650),
        )
    assert result.status == "exposure_slot_taken"
    assert result.reason == "instrument_open_root"


# --------------------------------------------------------------------------- #
# Concurrency (two independent committed sessions + barrier)
# --------------------------------------------------------------------------- #


async def _committed_open_root_count() -> int:
    async with AsyncSessionLocal() as db:
        return await BinanceDemoLedgerRepository(db).count_open_root_lifecycles()


async def _barrier_reserve(barrier, *, instrument_id, cid, cap):
    async with AsyncSessionLocal() as db:
        await barrier.wait()
        return await _reserve(db, instrument_id=instrument_id, cid=cid, cap=cap)


@pytest.mark.asyncio
async def test_same_symbol_concurrent_reservations_one_wins() -> None:
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844RACEUSDT")
    barrier = asyncio.Barrier(2)
    results = await asyncio.gather(
        _barrier_reserve(
            barrier, instrument_id=iid, cid=f"{_CID_PREFIX}race-1", cap=_HIGH_CAP
        ),
        _barrier_reserve(
            barrier, instrument_id=iid, cid=f"{_CID_PREFIX}race-2", cap=_HIGH_CAP
        ),
    )
    statuses = sorted(r.status for r in results)
    assert statuses == ["exposure_slot_taken", "reserved"]
    # Exactly one committed planned root exists for this instrument.
    async with AsyncSessionLocal() as db:
        roots = await db.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(
                BinanceDemoOrderLedger.instrument_id == iid,
                BinanceDemoOrderLedger.parent_client_order_id.is_(None),
            )
        )
    assert roots == 1


@pytest.mark.asyncio
async def test_different_symbol_global_cap_one_admits_one() -> None:
    async with AsyncSessionLocal() as db:
        iid1 = await _instrument(db, "R844GCAP1USDT")
        iid2 = await _instrument(db, "R844GCAP2USDT")
    # The file lock keeps the table-wide committed open-root count stable, so a
    # baseline-relative cap leaves exactly one free slot for two symbols.
    base = await _committed_open_root_count()
    cap = base + 1
    barrier = asyncio.Barrier(2)
    results = await asyncio.gather(
        _barrier_reserve(
            barrier, instrument_id=iid1, cid=f"{_CID_PREFIX}gcap-1", cap=cap
        ),
        _barrier_reserve(
            barrier, instrument_id=iid2, cid=f"{_CID_PREFIX}gcap-2", cap=cap
        ),
    )
    reserved = [r for r in results if r.status == "reserved"]
    taken = [r for r in results if r.status == "exposure_slot_taken"]
    assert len(reserved) == 1
    assert len(taken) == 1
    assert taken[0].reason == "global_open_root_cap"


@pytest.mark.asyncio
async def test_raw_count_then_insert_toctou_caught_by_db_index() -> None:
    """Characterization of the pre-fix race: two writers both pass the count
    check and each attempt a raw root insert. Without the advisory-locked
    reservation the counts do not serialize, so the partial-unique DB index is
    the last line of defense — exactly one insert commits, the other raises
    IntegrityError (which ``reserve_root_planned`` normalizes to a slot-taken)."""
    async with AsyncSessionLocal() as db:
        iid = await _instrument(db, "R844TOCTOUUSDT")

    barrier = asyncio.Barrier(2)
    outcomes: list[str] = []

    async def _racer(cid: str) -> None:
        async with AsyncSessionLocal() as db:
            repo = BinanceDemoLedgerRepository(db)
            # Legacy pattern: read the count first (no lock)…
            await repo.has_open_root_lifecycle_for_instrument(
                product="spot", instrument_id=iid
            )
            await barrier.wait()  # …both writers pass the check before either insert
            try:
                await repo.insert_planned(
                    instrument_id=iid,
                    product="spot",
                    venue_host=_HOST,
                    client_order_id=cid,
                    side="BUY",
                    order_type="MARKET",
                    qty=Decimal("1"),
                    price=None,
                    now=_NOW,
                )
                await db.commit()
                outcomes.append("committed")
            except IntegrityError:
                await db.rollback()
                outcomes.append("rejected")

    await asyncio.gather(
        _racer(f"{_CID_PREFIX}toctou-1"), _racer(f"{_CID_PREFIX}toctou-2")
    )
    assert sorted(outcomes) == ["committed", "rejected"]
