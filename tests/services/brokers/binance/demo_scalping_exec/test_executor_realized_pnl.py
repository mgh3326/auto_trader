"""ROB-XXX Phase 1 변경 A — close 행 realized_pnl_usdt durable 기록.

close 행 extra_metadata['realized_pnl_usdt']에 net PnL이 기록되고,
open 행은 미기록이며, 손실 라운드트립이 daily_loss_budget 게이트를 활성화함을 검증.
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

# ---------------------------------------------------------------------------
# Fixture block — copied verbatim from test_executor_analytics.py lines 30-146
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

from sqlalchemy import select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    ScalpingRiskLimits,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.ledger_state import load_ledger_snapshot


@pytest.mark.asyncio
async def test_close_row_carries_realized_pnl_open_row_does_not(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="100.40")
    md = _MD([100.0, 100.4])  # 2nd poll crosses TP
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("RPNLWINUSDT"),
        market_data=md,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_monitored(
        _intent("RPNLWINUSDT"), confirm=True, max_poll_count=5
    )
    assert result.status == "reconciled"

    analytics = await ScalpTradeAnalyticsService(db_session).get_by_open_client_order_id(
        result.open_client_order_id
    )
    close_row = await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == result.close_client_order_id
        )
    )
    open_row = await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == result.open_client_order_id
        )
    )
    # close row carries the signed net PnL, equal to the analytics net PnL.
    assert close_row.extra_metadata is not None
    assert "realized_pnl_usdt" in close_row.extra_metadata
    assert (
        Decimal(close_row.extra_metadata["realized_pnl_usdt"])
        == analytics.net_pnl_usdt
    )
    # open row is NOT stamped — single-count for _realized_loss_today.
    assert "realized_pnl_usdt" not in (open_row.extra_metadata or {})


@pytest.mark.asyncio
async def test_losing_round_trip_feeds_daily_loss_budget_gate(db_session) -> None:
    client = _FakeFutures(open_px="100", close_px="99")  # BUY then exit lower → loss
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_Ref(),
        now=_NOW,
        limits=_limits("RPNLLOSSUSDT"),
        market_data=None,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute(_intent("RPNLLOSSUSDT"), confirm=True)  # immediate open+close
    assert result.status == "reconciled"

    snapshot = await load_ledger_snapshot(
        BinanceDemoLedgerService(db_session),
        product="usdm_futures",
        symbol="RPNLLOSSUSDT",
        now=_NOW,
    )
    # gross loss ~ (99-100)*0.1 = -0.1 plus fees → realized loss >= 0.09.
    assert snapshot.realized_loss_today_usdt >= Decimal("0.09")

    decision = evaluate_risk(
        product="usdm_futures",
        symbol="RPNLLOSSUSDT",
        side="BUY",
        target_notional_usdt=Decimal("10"),
        limits=ScalpingRiskLimits(
            allowlist=frozenset({"RPNLLOSSUSDT"}),
            excluded=frozenset(),
            daily_loss_budget_usdt=Decimal("0.05"),
        ),
        ledger=snapshot,
        market=MarketConditions(
            spread_bps=Decimal("1"),
            data_age_seconds=1.0,
            spot_free_base_qty=Decimal("0"),
        ),
    )
    assert "daily_loss_budget_exhausted" in decision.reason_codes
