"""ROB-307 follow-up — tests for the bounded app-managed monitor exit.

execute_monitored opens, then polls the bookTicker within a bounded window
and MARKET-closes on TP/SL cross (or failsafe-closes at window end) — it
always ends flat in-run (no unattended position, no broker-side bracket).
Broker + market data faked; ledger is the real service on the test DB.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ScalpingRiskLimits,
)
from app.services.brokers.binance.demo_scalping.market_data import BookTicker
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference

_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)
_REF = SymbolReference(
    price=Decimal("100"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.01"),
)
# ROB-841: execute_monitored now fails closed without a server market snapshot.
_FRESH_MARKET = MarketConditions(
    spread_bps=Decimal("2"),
    data_age_seconds=5.0,
    spot_free_base_qty=Decimal("0"),
)


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
        entry_reference_price=Decimal("100"),
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0.5"),
        reason_codes=("enter_long_breakout",),
        source_candle_close_time_ms=1,
        evaluated_at_ms=2,
    )


class _FakeRef:
    async def fetch(self, product, symbol):
        return _REF

    async def aclose(self):
        return None


class _FakeMD:
    """Returns scripted mid prices (one per poll) as bid==ask bookTickers."""

    def __init__(self, prices):
        self._prices = [Decimal(str(p)) for p in prices]
        self.calls = 0

    async def fetch_book_ticker(self, product, symbol):
        p = self._prices[min(self.calls, len(self._prices) - 1)]
        self.calls += 1
        return BookTicker(bid=p, ask=p)


class _Order:
    def __init__(self, status, coid, broker="b1"):
        self.status = status
        self.client_order_id = coid
        self.broker_order_id = broker
        self.executed_qty = Decimal("0.1")


class _OpenOrders:
    def __init__(self, orders):
        self.orders = orders


class _Balance:
    def __init__(self, free):
        self.free = free


class _Position:
    def __init__(self, amt):
        self.position_amt = amt
        self.is_flat = amt == 0


class _FakeSpot:
    def __init__(self):
        self.submits = []
        self._free = Decimal("0")

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
        self._free = qty if side == "BUY" else Decimal("0")
        return _Order("FILLED", client_order_id)

    async def get_asset_balance(self, *, asset):
        return _Balance(self._free)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


class _FakeFutures:
    def __init__(self):
        self.submits = []
        self._amt = Decimal("0")

    async def get_position_mode(self):
        return type("M", (), {"is_hedge_mode": False})()

    async def set_leverage(self, *, symbol, leverage):
        return type("L", (), {"leverage": 1})()

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
        self.submits.append((side, reduce_only))
        self._amt = (
            (qty if side == "BUY" else -qty) if not reduce_only else Decimal("0")
        )
        return _Order("FILLED", client_order_id)

    async def get_order(self, *, symbol, client_order_id):
        return _Order("FILLED", client_order_id)

    async def get_position(self, *, symbol):
        return _Position(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


def _executor(product, client, md, db_session, symbol):
    return DemoScalpingExecutor(
        product=product,
        client=client,
        session=db_session,
        reference=_FakeRef(),
        now=_NOW,
        limits=_limits_for(symbol),
        market_data=md,
        poll_delay_seconds=0.0,
    )


# entry 100; tp_bps=30 -> tp=100.30; sl_bps=20 -> sl=99.80
@pytest.mark.asyncio
async def test_spot_monitor_take_profit(db_session) -> None:
    md = _FakeMD([100.0, 100.1, 100.35])  # 3rd poll crosses TP
    client = _FakeSpot()
    result = await _executor(
        "spot", client, md, db_session, "MONSPOTAUSDT"
    ).execute_monitored(
        _intent("spot", "MONSPOTAUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "take_profit"
    assert client.submits == ["BUY", "SELL"]


@pytest.mark.asyncio
async def test_spot_monitor_stop_loss(db_session) -> None:
    md = _FakeMD([100.0, 99.9, 99.75])  # crosses SL
    client = _FakeSpot()
    result = await _executor(
        "spot", client, md, db_session, "MONSPOTBUSDT"
    ).execute_monitored(
        _intent("spot", "MONSPOTBUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "stop_loss"


@pytest.mark.asyncio
async def test_spot_monitor_timeout_failsafe_close(db_session) -> None:
    md = _FakeMD([100.0, 100.05, 100.1])  # never crosses; bounded -> timeout
    client = _FakeSpot()
    result = await _executor(
        "spot", client, md, db_session, "MONSPOTCUSDT"
    ).execute_monitored(
        _intent("spot", "MONSPOTCUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=3,
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "timeout"
    assert client.submits == ["BUY", "SELL"]  # failsafe close still flattens


@pytest.mark.asyncio
async def test_futures_monitor_take_profit_flat(db_session) -> None:
    md = _FakeMD([100.0, 100.4])
    client = _FakeFutures()
    result = await _executor(
        "usdm_futures", client, md, db_session, "MONFUTAUSDT"
    ).execute_monitored(
        _intent("usdm_futures", "MONFUTAUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "take_profit"
    assert result.final_flat is True
    assert [s[1] for s in client.submits] == [False, True]  # open then reduceOnly close


@pytest.mark.asyncio
async def test_monitor_dry_run_places_nothing(db_session) -> None:
    md = _FakeMD([100.0])
    client = _FakeSpot()
    result = await _executor(
        "spot", client, md, db_session, "MONDRYUSDT"
    ).execute_monitored(
        _intent("spot", "MONDRYUSDT"),
        confirm=False,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    assert result.status == "dry_run"
    assert client.submits == []


class _RaisingMD:
    """bookTicker poll raises (simulates timeout / rate-limit / network)."""

    async def fetch_book_ticker(self, product, symbol):
        raise RuntimeError("bookTicker poll failed (network)")


@pytest.mark.asyncio
async def test_spot_monitor_error_still_closes_flat(db_session) -> None:
    client = _FakeSpot()
    result = await _executor(
        "spot", client, _RaisingMD(), db_session, "MONERRSPOTUSDT"
    ).execute_monitored(
        _intent("spot", "MONERRSPOTUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    # Open succeeded, monitor raised -> still closed + reconciled flat.
    assert result.status == "reconciled"
    assert result.exit_reason == "monitor_error"
    assert result.monitor_error is not None and "network" in result.monitor_error
    assert client.submits == ["BUY", "SELL"]  # failsafe close ran despite the error


@pytest.mark.asyncio
async def test_futures_monitor_error_still_reduce_only_closes_flat(db_session) -> None:
    client = _FakeFutures()
    result = await _executor(
        "usdm_futures", client, _RaisingMD(), db_session, "MONERRFUTUSDT"
    ).execute_monitored(
        _intent("usdm_futures", "MONERRFUTUSDT"),
        confirm=True,
        market=_FRESH_MARKET,
        max_poll_count=5,
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "monitor_error"
    assert result.final_flat is True
    # open (reduce_only False) then reduceOnly close (True) even after the error.
    assert [s[1] for s in client.submits] == [False, True]
