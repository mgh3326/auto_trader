"""ROB-307 PR2 — tests for the one-shot Demo executor (mocked broker).

The executor consumes an OrderIntent, re-checks risk against the live
ledger, and drives a full small Demo lifecycle to flat / open-orders-0,
writing the ledger lifecycle and reconciling. Broker I/O is faked (no
network); the ledger is the real service on the test DB. Covers spot +
futures happy paths, the futures NEW-status poll (ROB-305 §4), the
risk-block abort (no broker call), and the dirty-reconcile anomaly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoExecutionIdentity,
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
# ROB-841: the executor now fails closed without a server-derived market
# snapshot, so every execute()/execute_monitored() call supplies a fresh,
# tight one (spread/age well within the gates) unless it is testing the gates.
_FRESH_MARKET = MarketConditions(
    spread_bps=Decimal("2"),
    data_age_seconds=5.0,
    spot_free_base_qty=Decimal("0"),
)


# The shared db_session is never rolled back, so the 3 real allowlisted
# symbols carry residue across runs. Happy-path tests use a UNIQUE symbol
# with a per-test allowlist so they are deterministic regardless of residue;
# generous caps avoid the global/daily gates. The risk-block test keeps the
# defaults to exercise the real allowlist gate.
def _limits_for(symbol: str) -> ScalpingRiskLimits:
    return ScalpingRiskLimits(
        allowlist=frozenset({symbol}),
        excluded=frozenset(),
        global_open_lifecycle_cap=10_000,
        daily_order_count_cap=10_000,
        daily_loss_budget_usdt=Decimal("1000000"),
    )


def _intent(product: str, symbol: str, side: str = "BUY") -> OrderIntent:
    return OrderIntent(
        product=product,
        symbol=symbol,
        side=side,
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


class _FakeReference:
    def __init__(self, ref: SymbolReference) -> None:
        self._ref = ref

    async def fetch(self, product, symbol) -> SymbolReference:
        return self._ref

    async def aclose(self) -> None:  # pragma: no cover - parity with real
        return None


class _Order:
    def __init__(self, status, coid, broker_id=None, executed_qty=Decimal("7.3")):
        self.status = status
        self.client_order_id = coid
        # ROB-844: distinct orders get distinct broker ids (Binance never
        # replays an orderId). Derive from the unique per-leg coid so the
        # open + close legs of one round trip do not collide on the new
        # (product, venue_host, broker_order_id) ack-uniqueness index.
        self.broker_order_id = broker_id if broker_id is not None else f"bk-{coid}"
        self.executed_qty = executed_qty


class _OpenOrders:
    def __init__(self, orders):
        self.orders = orders


class _Balance:
    def __init__(self, free):
        self.asset = "XRP"
        self.free = free
        self.locked = Decimal("0")


class _Position:
    def __init__(self, amt):
        self.position_amt = amt
        self.is_flat = amt == 0
        self.entry_price = Decimal("1.36")
        self.leverage = 1


class _PositionMode:
    is_hedge_mode = False


class _Leverage:
    def __init__(self, lev=1):
        self.leverage = lev


class _FakeSpotClient:
    """Spot MARKET fills immediately; SELL closes the free base balance."""

    def __init__(self, *, free_after_buy=Decimal("7.3")):
        self.submits: list[dict] = []
        self._free = Decimal("0")
        self._free_after_buy = free_after_buy
        self._open: list = []

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
        self.submits.append({"side": side, "qty": qty, "confirm": confirm})
        if side == "BUY":
            self._free = self._free_after_buy
        else:
            self._free = Decimal("0")
        return _Order("FILLED", client_order_id)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders(self._open)

    async def get_asset_balance(self, *, asset):
        return _Balance(self._free)


class _FakeFuturesClient:
    """Futures: optional NEW-then-FILLED poll; reduceOnly close to flat."""

    def __init__(self, *, open_status="FILLED", fills_after_polls=0):
        self.submits: list[dict] = []
        self.leverage_calls: list[int] = []
        self._amt = Decimal("0")
        self._open_status = open_status
        self._fills_after_polls = fills_after_polls
        self._poll_count = 0

    async def get_position_mode(self):
        return _PositionMode()

    async def set_leverage(self, *, symbol, leverage):
        self.leverage_calls.append(leverage)
        return _Leverage(leverage)

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
        reduce_only=False,
        confirm=False,
    ):
        self.submits.append(
            {"side": side, "reduce_only": reduce_only, "confirm": confirm}
        )
        if reduce_only:
            self._amt = Decimal("0")
        else:
            self._amt = qty if side == "BUY" else -qty
            self._poll_count = 0
        return _Order(
            self._open_status if not reduce_only else "FILLED", client_order_id
        )

    async def get_order(self, *, symbol, client_order_id):
        self._poll_count += 1
        status = "FILLED" if self._poll_count >= self._fills_after_polls else "NEW"
        return _Order(status, client_order_id)

    async def get_position(self, *, symbol):
        return _Position(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


async def _instrument(db_session, symbol, product) -> int:
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
    return inst.id


_SPOT_REF = SymbolReference(
    price=Decimal("1.36"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.0001"),
)
_FUT_REF = SymbolReference(
    price=Decimal("1.36"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.0001"),
)


@pytest.mark.asyncio
async def test_spot_happy_path_reconciles_flat(db_session) -> None:
    client = _FakeSpotClient(free_after_buy=Decimal("7.3"))
    executor = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for("EXESPOTAUSDT"),
    )
    result = await executor.execute(
        _intent("spot", "EXESPOTAUSDT"), confirm=True, market=_FRESH_MARKET
    )
    assert result.status == "reconciled"
    assert result.final_open_orders == 0
    # A BUY then a SELL were submitted with confirm=True.
    sides = [s["side"] for s in client.submits]
    assert sides == ["BUY", "SELL"]
    assert all(s["confirm"] for s in client.submits)


@pytest.mark.asyncio
async def test_dry_run_places_no_order(db_session) -> None:
    client = _FakeSpotClient()
    executor = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for("EXESPOTBUSDT"),
    )
    result = await executor.execute(
        _intent("spot", "EXESPOTBUSDT"), confirm=False, market=_FRESH_MARKET
    )
    assert result.status == "dry_run"
    assert client.submits == []  # zero broker mutation


@pytest.mark.asyncio
async def test_risk_block_aborts_without_broker_call(db_session) -> None:
    client = _FakeSpotClient()
    # Symbol outside the allowlist → risk blocks before any order.
    executor = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
    )
    result = await executor.execute(
        _intent("spot", "ETHUSDT"), confirm=True, market=_FRESH_MARKET
    )
    assert result.status == "blocked"
    assert "symbol_not_allowlisted" in result.reason_codes
    assert client.submits == []


@pytest.mark.asyncio
async def test_futures_happy_path_pins_leverage_and_reconciles_flat(db_session) -> None:
    client = _FakeFuturesClient(open_status="FILLED")
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_FakeReference(_FUT_REF),
        now=_NOW,
        limits=_limits_for("EXEFUTAUSDT"),
    )
    result = await executor.execute(
        _intent("usdm_futures", "EXEFUTAUSDT"), confirm=True, market=_FRESH_MARKET
    )
    assert result.status == "reconciled"
    assert result.final_flat is True
    assert client.leverage_calls == [1]  # pinned to 1x
    # open (reduce_only False) then close (reduce_only True)
    assert [s["reduce_only"] for s in client.submits] == [False, True]


@pytest.mark.asyncio
async def test_futures_new_status_polls_to_filled(db_session) -> None:
    client = _FakeFuturesClient(open_status="NEW", fills_after_polls=2)
    executor = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_FakeReference(_FUT_REF),
        now=_NOW,
        poll_delay_seconds=0.0,
        limits=_limits_for("EXEFUTBUSDT"),
    )
    result = await executor.execute(
        _intent("usdm_futures", "EXEFUTBUSDT"), confirm=True, market=_FRESH_MARKET
    )
    assert result.status == "reconciled"
    assert result.final_flat is True


def _execution_identity(*, intent_hash: str = "a" * 64) -> DemoExecutionIdentity:
    return DemoExecutionIdentity.from_verified_metadata(
        decision_id="decision-rob845-binance",
        idempotency_key="paper-binance-" + "1" * 32,
        immutable_metadata={
            "experiment_id": "experiment-1",
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "strategy_version_id": "strategy-v1",
            "intent_hash": intent_hash,
            "strategy_hash": "b" * 64,
            "config_hash": "c" * 64,
            "policy_hash": "d" * 64,
            "market_snapshot_id": "snapshot-1",
            "market_snapshot_hash": "e" * 64,
            "market_snapshot_as_of": "2026-07-13T01:00:00+00:00",
            "market_snapshot_source": "binance_public_spot",
        },
    )


@pytest.mark.asyncio
async def test_verified_identity_uses_deterministic_native_ids_and_metadata(
    db_session,
) -> None:
    symbol = "ROB845EXECUSDT"
    identity = _execution_identity()
    same = _execution_identity()
    assert identity == same
    assert identity.root_client_order_id.startswith("rob845r-")
    assert identity.close_client_order_id.startswith("rob845c-")
    assert len(identity.root_client_order_id) <= 36
    assert len(identity.close_client_order_id) <= 36

    client = _FakeSpotClient(free_after_buy=Decimal("7.3"))
    executor = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for(symbol),
        execution_identity=identity,
    )
    result = await executor.execute(
        _intent("spot", symbol), confirm=True, market=_FRESH_MARKET
    )

    assert result.status == "reconciled"
    assert result.open_client_order_id == identity.root_client_order_id
    assert result.close_client_order_id == identity.close_client_order_id
    rows = list(
        (
            await db_session.scalars(
                select(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id.in_(
                        [identity.root_client_order_id, identity.close_client_order_id]
                    )
                )
            )
        ).all()
    )
    assert len(rows) == 2
    for row in rows:
        metadata = row.extra_metadata["paper_execution_identity"]
        assert metadata["decision_id"] == "decision-rob845-binance"
        assert metadata["root_client_order_id"] == identity.root_client_order_id
        assert metadata["close_client_order_id"] == identity.close_client_order_id
        assert metadata["native_intent"]["symbol"] == symbol


@pytest.mark.asyncio
async def test_terminal_verified_identity_replays_before_market_preflight(
    db_session,
) -> None:
    symbol = "ROB845REPLAYUSDT"
    identity = _execution_identity()
    first_client = _FakeSpotClient(free_after_buy=Decimal("7.3"))
    first = DemoScalpingExecutor(
        product="spot",
        client=first_client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for(symbol),
        execution_identity=identity,
    )
    initial = await first.execute(
        _intent("spot", symbol), confirm=True, market=_FRESH_MARKET
    )
    assert initial.status == "reconciled"
    await db_session.commit()

    replay_client = _FakeSpotClient()
    replay_executor = DemoScalpingExecutor(
        product="spot",
        client=replay_client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW + dt.timedelta(hours=1),
        limits=_limits_for(symbol),
        execution_identity=identity,
    )
    replay = await replay_executor.execute(
        _intent("spot", symbol), confirm=True, market=None
    )

    assert replay.status == "reconciled"
    assert replay.replayed is True
    assert replay.open_client_order_id == identity.root_client_order_id
    assert replay.close_client_order_id == identity.close_client_order_id
    assert replay_client.submits == []


@pytest.mark.asyncio
async def test_verified_identity_collision_blocks_before_market_preflight(
    db_session,
) -> None:
    symbol = "ROB845COLLIDEUSDT"
    first_identity = _execution_identity()
    first = DemoScalpingExecutor(
        product="spot",
        client=_FakeSpotClient(free_after_buy=Decimal("7.3")),
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for(symbol),
        execution_identity=first_identity,
    )
    await first.execute(_intent("spot", symbol), confirm=True, market=_FRESH_MARKET)
    await db_session.commit()

    collision_client = _FakeSpotClient()
    collision = DemoScalpingExecutor(
        product="spot",
        client=collision_client,
        session=db_session,
        reference=_FakeReference(_SPOT_REF),
        now=_NOW,
        limits=_limits_for(symbol),
        execution_identity=_execution_identity(intent_hash="f" * 64),
    )
    result = await collision.execute(_intent("spot", symbol), confirm=True, market=None)

    assert result.status == "blocked"
    assert result.reason_codes == ("idempotency_collision",)
    assert collision_client.submits == []
