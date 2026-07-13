"""ROB-844 — the executor only submits to the broker as the reservation winner.

Two guarantees:

* Even when the advisory read-side ledger snapshot is stale/clean (the exact
  TOCTOU this ticket closes), the atomic reservation catches the taken exposure
  slot and blocks the open with ``EXPOSURE_SLOT_TAKEN`` and ZERO broker POST.
* Two concurrent executors for the same symbol produce at most one broker open
  submit; the loser returns a structured blocked result and never calls the
  broker.

Broker I/O is faked (no network); the ledger is the real service on the test DB.
The directory ``conftest.py`` serializes this file with the reservation family so
the table-wide open-root count is stable (ROB-842).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import AsyncSessionLocal
from app.core.db import engine as shared_engine
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    ReasonCode,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec import executor as executor_mod
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_HOST = "demo-api.binance.com"
_CID_PREFIX = "rob844exec-"
_CREDENTIAL_FINGERPRINT = "sha256:" + "31" * 32
_REF = SymbolReference(
    price=Decimal("1.36"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.0001"),
)
_FRESH_MARKET = MarketConditions(
    spread_bps=Decimal("2"), data_age_seconds=5.0, spot_free_base_qty=Decimal("0")
)


def _limits(symbol: str) -> ScalpingRiskLimits:
    return ScalpingRiskLimits(
        allowlist=frozenset({symbol}),
        excluded=frozenset(),
        global_open_lifecycle_cap=1_000_000,
        daily_order_count_cap=1_000_000,
        daily_loss_budget_usdt=Decimal("1000000"),
        cooldown_seconds=0,
    )


def _intent(symbol: str) -> OrderIntent:
    return OrderIntent(
        product="spot",
        symbol=symbol,
        side="BUY",
        order_type="MARKET",
        target_notional_usdt=Decimal("10"),
        entry_reference_price=Decimal("1.36"),
        tp_price=Decimal("1.40"),
        sl_price=Decimal("1.33"),
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1_779_000_000_000,
        evaluated_at_ms=1_779_000_001_000,
    )


class _Order:
    def __init__(self, status, coid):
        self.status = status
        self.client_order_id = coid
        self.broker_order_id = f"bk-{coid}"
        self.executed_qty = Decimal("7.3")


class _Balance:
    def __init__(self, free):
        self.asset = "XRP"
        self.free = free
        self.locked = Decimal("0")


class _OpenOrders:
    def __init__(self, orders):
        self.orders = orders


class _FakeReference:
    def __init__(self, ref):
        self._ref = ref

    async def fetch(self, product, symbol):
        return self._ref

    async def aclose(self):
        return None


class _GatedSpotClient:
    """Spot MARKET fills immediately; the close's balance read can be gated so
    the winner keeps its root open (``filled``) while the loser reserves."""

    def __init__(self, *, free_after_buy=Decimal("7.3"), close_gate=None):
        self.submits: list[str] = []
        self._free = Decimal("0")
        self._free_after_buy = free_after_buy
        self._close_gate = close_gate

    credential_fingerprint = _CREDENTIAL_FINGERPRINT

    async def submit_order(
        self,
        *,
        symbol,
        side,
        order_type,
        qty,
        client_order_id=None,
        price=None,
        time_in_force=None,
        confirm=False,
    ):
        self.submits.append(side)
        self._free = self._free_after_buy if side == "BUY" else Decimal("0")
        return _Order("FILLED", client_order_id)

    async def get_asset_balance(self, *, asset):
        if self._close_gate is not None:
            await self._close_gate.wait()
        return _Balance(self._free)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


async def _clean(symbol: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id.like(f"{_CID_PREFIX}%")
            )
        )
        await db.commit()


async def _instrument(symbol: str) -> int:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(
            select(CryptoInstrument).where(
                CryptoInstrument.venue == "binance",
                CryptoInstrument.product == "spot",
                CryptoInstrument.venue_symbol == symbol,
            )
        )
        if existing is not None:
            return existing.id
        inst = CryptoInstrument(
            venue="binance",
            product="spot",
            venue_symbol=symbol,
            base_asset=symbol.replace("USDT", ""),
            quote_asset="USDT",
            status="active",
        )
        db.add(inst)
        await db.flush()
        iid = inst.id
        await db.commit()
        return iid


@pytest_asyncio.fixture(autouse=True)
async def _cleanup():
    yield
    await _clean("")


@pytest.mark.asyncio
async def test_stale_snapshot_reservation_blocks_with_zero_broker_post(
    monkeypatch,
) -> None:
    """The TOCTOU: a clean advisory snapshot passes the risk gate, but the atomic
    reservation sees the already-open root and blocks with zero broker POST."""
    symbol = "R844EXSTALEUSDT"
    iid = await _instrument(symbol)
    # A committed open root already occupies the slot (the "winner" of a race).
    async with AsyncSessionLocal() as db:
        await BinanceDemoLedgerService(db).record_planned(
            instrument_id=iid,
            product="spot",
            venue_host=_HOST,
            client_order_id=f"{_CID_PREFIX}stale-open",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("1"),
            price=None,
            now=_NOW,
        )
        await db.commit()

    # Force the read-side snapshot to be stale/clean, so the risk gate passes and
    # the reservation is the ONLY thing standing between us and a duplicate order.
    async def _clean_snapshot(*_args, **_kwargs):
        return LedgerSnapshot(
            has_open_lifecycle_for_symbol=False,
            global_open_lifecycle_count=0,
            orders_today=0,
            realized_loss_today_usdt=Decimal("0"),
            seconds_since_last_close_for_symbol=None,
        )

    monkeypatch.setattr(executor_mod, "load_ledger_snapshot", _clean_snapshot)

    client = _GatedSpotClient()
    async with AsyncSessionLocal() as session:
        executor = DemoScalpingExecutor(
            product="spot",
            client=client,
            session=session,
            reference=_FakeReference(_REF),
            now=_NOW,
            limits=_limits(symbol),
        )
        result = await executor.execute(
            _intent(symbol), confirm=True, market=_FRESH_MARKET
        )

    assert result.status == "blocked"
    assert ReasonCode.EXPOSURE_SLOT_TAKEN in result.reason_codes
    assert client.submits == []  # ZERO broker POST for the reservation loser


@pytest.mark.asyncio
async def test_concurrent_executors_at_most_one_broker_submit() -> None:
    symbol = "R844EXRACEUSDT"
    await _instrument(symbol)  # pre-create so neither executor races the insert
    close_gate = asyncio.Event()
    barrier = asyncio.Barrier(2)

    async def _run(client):
        async with AsyncSessionLocal() as session:
            executor = DemoScalpingExecutor(
                product="spot",
                client=client,
                session=session,
                reference=_FakeReference(_REF),
                now=_NOW,
                limits=_limits(symbol),
            )
            await barrier.wait()
            return await executor.execute(
                _intent(symbol), confirm=True, market=_FRESH_MARKET
            )

    client_a = _GatedSpotClient(close_gate=close_gate)
    client_b = _GatedSpotClient(close_gate=close_gate)
    task_a = asyncio.create_task(_run(client_a))
    task_b = asyncio.create_task(_run(client_b))
    # The loser blocks at the reservation and returns first; the winner is gated
    # at its close (root still open) so the loser deterministically sees the slot
    # as taken. Release the winner once the loser has settled.
    _done, _pending = await asyncio.wait(
        {task_a, task_b}, return_when=asyncio.FIRST_COMPLETED
    )
    close_gate.set()
    result_a, result_b = await asyncio.gather(task_a, task_b)

    pairs = [(result_a, client_a), (result_b, client_b)]
    blocked = [(r, c) for r, c in pairs if r.status == "blocked"]
    winners = [(r, c) for r, c in pairs if r.status == "reconciled"]
    assert len(blocked) == 1
    assert len(winners) == 1
    loser_result, loser_client = blocked[0]
    winner_result, winner_client = winners[0]
    # Loser: zero broker POST, structured blocked reason (reservation or the
    # advisory gate that observed the committed root — both are "slot taken").
    assert loser_client.submits == []
    assert any(
        code
        in (
            ReasonCode.EXPOSURE_SLOT_TAKEN,
            ReasonCode.OPEN_LIFECYCLE_EXISTS,
            ReasonCode.GLOBAL_LIFECYCLE_CAP_REACHED,
        )
        for code in loser_result.reason_codes
    )
    # Winner: exactly one open (BUY) submit — the only broker open across both.
    assert winner_client.submits[0] == "BUY"
    async with AsyncSessionLocal() as db:
        root = await db.scalar(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.client_order_id
                == winner_result.open_client_order_id
            )
        )
    assert root is not None
    assert root.extra_metadata["credential_fingerprint"] == _CREDENTIAL_FINGERPRINT
    assert "RAW" not in repr(root.extra_metadata)


@pytest.mark.asyncio
async def test_pool_size_one_preflight_identity_reservation_and_transition_do_not_starve(
    monkeypatch,
) -> None:
    """Short independent sessions run sequentially; owner holds no first lease."""
    pool_engine = create_async_engine(
        shared_engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
        pool_pre_ping=True,
    )
    factory = async_sessionmaker(pool_engine, expire_on_commit=False)
    symbol = "R844POOLONEUSDT"
    try:
        client = _GatedSpotClient()
        async with factory() as owner:
            assert not owner.in_transaction()
            executor = DemoScalpingExecutor(
                product="spot",
                client=client,
                session=owner,
                reference=_FakeReference(_REF),
                now=_NOW,
                limits=_limits(symbol),
            )
            result = await asyncio.wait_for(
                executor.execute(_intent(symbol), confirm=True, market=_FRESH_MARKET),
                timeout=5,
            )
        assert result.status == "reconciled"
        assert client.submits == ["BUY", "SELL"]

        # Also exercise the reservation-loser path with the same one-connection
        # pool while forcing the advisory snapshot stale/clean. The authoritative
        # reservation still blocks and dispatches zero broker submit.
        blocked_symbol = "R844POOLBLOCKUSDT"
        async with factory() as seed_session:
            seed = BinanceDemoLedgerService(seed_session)
            instrument_id = await seed.resolve_or_create_instrument(
                venue="binance",
                product="spot",
                venue_symbol=blocked_symbol,
                base_asset="R844POOLBLOCK",
                quote_asset="USDT",
            )
            seeded = await seed.reserve_root_planned(
                instrument_id=instrument_id,
                product="spot",
                venue_host=_HOST,
                client_order_id=f"{_CID_PREFIX}pool-blocker",
                side="BUY",
                order_type="MARKET",
                qty=Decimal("1"),
                price=None,
                global_open_root_cap=1_000_000,
                now=_NOW,
            )
            assert seeded.status == "reserved"

        async def _clean_snapshot(*_args, **_kwargs):
            return LedgerSnapshot(
                has_open_lifecycle_for_symbol=False,
                global_open_lifecycle_count=0,
                orders_today=0,
                realized_loss_today_usdt=Decimal("0"),
                seconds_since_last_close_for_symbol=None,
            )

        monkeypatch.setattr(executor_mod, "load_ledger_snapshot", _clean_snapshot)
        loser_client = _GatedSpotClient()
        async with factory() as loser_owner:
            assert not loser_owner.in_transaction()
            loser = DemoScalpingExecutor(
                product="spot",
                client=loser_client,
                session=loser_owner,
                reference=_FakeReference(_REF),
                now=_NOW,
                limits=_limits(blocked_symbol),
            )
            blocked = await asyncio.wait_for(
                loser.execute(
                    _intent(blocked_symbol), confirm=True, market=_FRESH_MARKET
                ),
                timeout=5,
            )
        assert blocked.status == "blocked"
        assert ReasonCode.EXPOSURE_SLOT_TAKEN in blocked.reason_codes
        assert loser_client.submits == []
    finally:
        await pool_engine.dispose()
