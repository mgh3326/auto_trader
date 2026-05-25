"""ROB-313 — the executor writes a correct scalp_trade_analytics row at reconcile.

Verifies the cost-capture wiring end-to-end: entry/exit avg fill prices are
captured, economics computed (slippage exact, fees estimated), and one
round-trip row persisted. Broker + market data faked; ledger + analytics are
the real services on the test DB.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.market_data import BookTicker
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.analytics import (
    ScalpTradeAnalyticsService,
)
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


def _limits(symbol: str) -> ScalpingRiskLimits:
    return ScalpingRiskLimits(
        allowlist=frozenset({symbol}),
        excluded=frozenset(),
        global_open_lifecycle_cap=10_000,
        daily_order_count_cap=10_000,
        daily_loss_budget_usdt=Decimal("1000000"),
    )


def _intent(symbol: str) -> OrderIntent:
    return OrderIntent(
        product="usdm_futures",
        symbol=symbol,
        side="BUY",
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


class _Ref:
    async def fetch(self, product, symbol):
        return _REF

    async def aclose(self):
        return None


class _MD:
    def __init__(self, prices):
        self._p = [Decimal(str(x)) for x in prices]
        self.i = 0

    async def fetch_book_ticker(self, product, symbol):
        p = self._p[min(self.i, len(self._p) - 1)]
        self.i += 1
        return BookTicker(bid=p, ask=p)


class _Sub:
    def __init__(self, status, coid, avg, qty=Decimal("0.1")):
        self.status = status
        self.client_order_id = coid
        self.broker_order_id = "b1"
        self.avg_price = avg
        self.executed_qty = qty


class _OO:
    def __init__(self, orders):
        self.orders = orders


class _Pos:
    def __init__(self, amt):
        self.position_amt = amt
        self.is_flat = amt == 0


class _FakeFutures:
    """Open fills at ``open_px``; reduceOnly close fills at ``close_px``."""

    def __init__(self, open_px, close_px):
        self.open_px = Decimal(str(open_px))
        self.close_px = Decimal(str(close_px))
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
        if reduce_only:
            self._amt = Decimal("0")
            return _Sub("FILLED", client_order_id, self.close_px, qty)
        self._amt = qty if side == "BUY" else -qty
        return _Sub("FILLED", client_order_id, self.open_px, qty)

    async def get_order(self, *, symbol, client_order_id):
        return _Sub("FILLED", client_order_id, self.open_px)

    async def get_position(self, *, symbol):
        return _Pos(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OO([])


@pytest.mark.asyncio
async def test_monitored_take_profit_writes_correct_analytics_row(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])  # 2nd poll crosses TP (tp_bps=30 -> tp=100.30)
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("ANAFUTAUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("ANAFUTAUSDT"), confirm=True, max_poll_count=5
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "take_profit"

    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.side == "BUY"
    assert row.entry_price == Decimal("100")  # captured open avg fill
    assert row.exit_price == Decimal("100.40")  # captured close avg fill
    assert row.fee_rate_bps == Decimal("5")
    assert row.entry_slippage_bps == Decimal("0")  # fill == reference
    # gross = (100.40 - 100) * 0.1 = 0.04 ; net is smaller once fees apply.
    assert row.gross_pnl_usdt == Decimal("0.04")
    assert row.net_pnl_usdt is not None
    assert row.net_pnl_usdt < row.gross_pnl_usdt
    assert row.exit_reason == "take_profit"


@pytest.mark.asyncio
async def test_dry_run_writes_no_analytics_row(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0])
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("ANADRYUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("ANADRYUSDT"), confirm=False, max_poll_count=5
    )
    assert result.status == "dry_run"
    # No open client order id on a dry run → no analytics row.
    assert result.open_client_order_id is None
