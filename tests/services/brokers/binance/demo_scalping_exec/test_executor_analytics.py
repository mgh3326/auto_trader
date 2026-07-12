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

from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
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

# ROB-841: the executor fails closed without a server market snapshot; supply a
# fresh, tight one for calls that are not themselves testing the market gates.
_FRESH_MARKET = MarketConditions(
    spread_bps=Decimal("2"),
    data_age_seconds=5.0,
    spot_free_base_qty=Decimal("0"),
)

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
        _intent("ANAFUTAUSDT"), confirm=True, market=_FRESH_MARKET, max_poll_count=5
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


class _FakeFuturesOpenPollFill:
    """Open submit returns NEW/avg=0; the open ``get_order`` poll proves the
    fill at ``filled_px``. Close reduceOnly fills immediately at ``close_px``.
    Exercises the ROB-315 0b fix: the recorded entry price must come from the
    fill-proven poll, never the reference price."""

    def __init__(self, filled_px, close_px):
        self.filled_px = Decimal(str(filled_px))
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
        return _Sub("NEW", client_order_id, Decimal("0"), Decimal("0"))

    async def get_order(self, *, symbol, client_order_id):
        return _Sub("FILLED", client_order_id, self.filled_px, Decimal("0.1"))

    async def get_position(self, *, symbol):
        return _Pos(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OO([])


class _FakeFuturesClosePollFill:
    """Open fills immediately at ``open_px``; the close reduceOnly submit
    returns NEW and is proven FILLED at ``close_px`` only by the close
    ``get_order`` poll. The recorded exit price must come from that poll."""

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
            return _Sub("NEW", client_order_id, Decimal("0"), Decimal("0"))
        self._amt = qty if side == "BUY" else -qty
        return _Sub("FILLED", client_order_id, self.open_px, qty)

    async def get_order(self, *, symbol, client_order_id):
        return _Sub("FILLED", client_order_id, self.close_px, Decimal("0.1"))

    async def get_position(self, *, symbol):
        return _Pos(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OO([])


class _FakeFuturesOpenNoPrice:
    """Open submit NEW/avg=0; ``get_order`` never proves FILLED (stays NEW),
    so the fill is proven only by a non-flat positionRisk — which carries NO
    usable fill price. The close fills at ``close_px``. ROB-315 0b: with no
    derivable entry fill price, analytics must record a partial row (entry
    price NULL, no economics), never fabricate it from the reference."""

    def __init__(self, close_px):
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
        return _Sub("NEW", client_order_id, Decimal("0"), Decimal("0"))

    async def get_order(self, *, symbol, client_order_id):
        return _Sub("NEW", client_order_id, Decimal("0"), Decimal("0"))

    async def get_position(self, *, symbol):
        return _Pos(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OO([])


@pytest.mark.asyncio
async def test_open_NEW_then_get_order_filled_uses_polled_entry_price(
    db_session,
) -> None:
    """Submit NEW/avgPrice=0, later get_order FILLED/avgPrice>0 → entry price
    is the polled fill (101), not the reference (100)."""
    client = _FakeFuturesOpenPollFill(filled_px="101", close_px="101")
    md = _MD([101.0, 101.4])  # 2nd poll crosses TP relative to ref=100
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("POLLENTRYUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("POLLENTRYUSDT"), confirm=True, market=_FRESH_MARKET, max_poll_count=5
    )
    assert result.status == "reconciled"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.entry_price == Decimal("101")  # polled fill, NOT ref price 100
    # BUY adverse slippage vs reference 100: (101-100)/100 * 10_000 = 100 bps.
    assert row.entry_slippage_bps == Decimal("100")


@pytest.mark.asyncio
async def test_close_NEW_then_get_order_filled_uses_polled_exit_price(
    db_session,
) -> None:
    """Close submit NEW, proven FILLED only by the close get_order poll →
    exit price + PnL use the polled close fill (100.5)."""
    client = _FakeFuturesClosePollFill(open_px="100", close_px="100.5")
    md = _MD([100.0, 100.4])  # cross TP so we close
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("POLLEXITUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("POLLEXITUSDT"), confirm=True, market=_FRESH_MARKET, max_poll_count=5
    )
    assert result.status == "reconciled"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    assert row.entry_price == Decimal("100")
    assert row.exit_price == Decimal("100.5")  # polled close fill, not NULL
    # gross = (100.5 - 100) * 0.1 = 0.05
    assert row.gross_pnl_usdt == Decimal("0.05")


@pytest.mark.asyncio
async def test_open_proven_by_position_without_price_records_partial_row(
    db_session,
) -> None:
    """Fill proven only by non-flat positionRisk (no order fill price) →
    partial analytics row: entry price NULL, no fabricated economics from the
    reference price."""
    client = _FakeFuturesOpenNoPrice(close_px="100.4")
    md = _MD([100.0, 100.4])
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("NOPRICEUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("NOPRICEUSDT"), confirm=True, market=_FRESH_MARKET, max_poll_count=5
    )
    assert result.status == "reconciled"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None  # round-trip is recorded (audit trail)
    assert row.entry_price is None  # NOT fabricated from reference price 100
    assert row.entry_notional_usdt is None
    assert row.gross_pnl_usdt is None
    assert row.net_pnl_usdt is None


class _MDBook:
    """Market data fake returning an explicit ``(bid, ask)`` sequence so the
    monitor's conservative price path + spread are deterministic."""

    def __init__(self, books):
        self._b = [(Decimal(str(x)), Decimal(str(y))) for x, y in books]
        self.i = 0

    async def fetch_book_ticker(self, product, symbol):
        bid, ask = self._b[min(self.i, len(self._b) - 1)]
        self.i += 1
        return BookTicker(bid=bid, ask=ask)


@pytest.mark.asyncio
async def test_monitored_run_captures_full_telemetry(db_session) -> None:
    """ROB-315 0c: MAE/MFE, spread@fill (entry from preflight market / exit
    from monitor book), holding time, and exit-slippage reference are all
    captured on a normal monitored round-trip."""
    client = _FakeFutures(open_px="100", close_px="100.40")
    # Conservative (bid) path 99.9 -> 100.4 (within sl=99.80..tp=100.30 band on
    # poll 1, crosses tp on poll 2).
    md = _MDBook([(99.9, 100.0), (100.4, 100.5)])
    market = MarketConditions(
        spread_bps=Decimal("10"),
        data_age_seconds=5.0,
        spot_free_base_qty=Decimal("0"),
    )
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("TELEUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("TELEUSDT"), confirm=True, market=market, max_poll_count=5
    )
    assert result.status == "reconciled"
    assert result.exit_reason == "take_profit"
    row = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    assert row is not None
    # MAE/MFE vs entry reference 100, over conservative path 99.9..100.4.
    assert row.mfe_bps == Decimal("40")  # (100.4-100)/100 * 10_000
    assert row.mae_bps == Decimal("-10")  # (99.9-100)/100 * 10_000
    assert row.entry_spread_bps == Decimal("10")  # from preflight market snapshot
    assert row.exit_spread_bps is not None and row.exit_spread_bps > 0
    assert row.holding_seconds is not None and row.holding_seconds >= 0
    assert row.exit_slippage_bps is not None  # exit reference now present


@pytest.mark.asyncio
async def test_preflight_blocks_on_wide_spread(db_session) -> None:
    """ROB-315 0c / D4: a spread above the 20 bps cap blocks the entry."""
    client = _FakeFutures(open_px="100", close_px="100")
    md = _MDBook([(100.0, 100.1)])
    market = MarketConditions(
        spread_bps=Decimal("25"),
        data_age_seconds=5.0,
        spot_free_base_qty=Decimal("0"),
    )
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("WIDEUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("WIDEUSDT"), confirm=True, market=market, max_poll_count=5
    )
    assert result.status == "blocked"
    assert "spread_too_wide" in result.reason_codes


@pytest.mark.asyncio
async def test_preflight_blocks_on_stale_data(db_session) -> None:
    """ROB-315 0c / D4: data older than the 120s cap blocks the entry."""
    client = _FakeFutures(open_px="100", close_px="100")
    md = _MDBook([(100.0, 100.1)])
    market = MarketConditions(
        spread_bps=Decimal("5"),
        data_age_seconds=300.0,
        spot_free_base_qty=Decimal("0"),
    )
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("STALEUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("STALEUSDT"), confirm=True, market=market, max_poll_count=5
    )
    assert result.status == "blocked"
    assert "stale_data" in result.reason_codes


@pytest.mark.asyncio
async def test_preflight_passes_within_market_bounds(db_session) -> None:
    """ROB-315 0c / D4: spread + data age within bounds do not block."""
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MDBook([(99.5, 99.6), (100.4, 100.5)])
    market = MarketConditions(
        spread_bps=Decimal("5"),
        data_age_seconds=10.0,
        spot_free_base_qty=Decimal("0"),
    )
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("OKUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("OKUSDT"), confirm=True, market=market, max_poll_count=5
    )
    assert result.status == "reconciled"


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
        _intent("ANADRYUSDT"), confirm=False, market=_FRESH_MARKET, max_poll_count=5
    )
    assert result.status == "dry_run"
    # No open client order id on a dry run → no analytics row.
    assert result.open_client_order_id is None
