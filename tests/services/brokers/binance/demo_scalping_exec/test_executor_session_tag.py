"""Task 1 — session_tag + signal_snapshot threaded through the executor.

Verifies that execute_monitored forwards session_tag/signal_snapshot to the
analytics row, and that callers omitting them get NULL (no regression).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.demo_scalping.contract import (
    ScalpingRiskLimits,
)
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
async def test_session_tag_and_signal_snapshot_recorded(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])
    ex = DemoScalpingExecutor(
        product="usdm_futures", client=client, session=db_session,
        reference=_Ref(), now=_NOW, limits=_limits("LLMTAGUSDT"),
        market_data=md, poll_delay_seconds=0.0,
    )
    snap = {"source": "llm", "rationale": "funding flip + oversold"}
    result = await ex.execute_monitored(
        _intent("LLMTAGUSDT"), confirm=True, max_poll_count=5,
        session_tag="llm", signal_snapshot=snap,
    )
    assert result.status == "reconciled"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.session_tag == "llm"
    assert row.signal_snapshot == snap


@pytest.mark.asyncio
async def test_session_tag_defaults_none_no_regression(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])
    ex = DemoScalpingExecutor(
        product="usdm_futures", client=client, session=db_session,
        reference=_Ref(), now=_NOW, limits=_limits("NOTAGUSDT"),
        market_data=md, poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(_intent("NOTAGUSDT"), confirm=True, max_poll_count=5)
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.session_tag is None
    assert row.signal_snapshot is None
