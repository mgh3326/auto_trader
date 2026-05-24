"""ROB-307 PR3 — tests for broker-side bracket placement (execute_bracket).

Unlike PR2's open+close-flat, the bracket path opens, places exchange-
native exits (futures: STOP_MARKET + TAKE_PROFIT_MARKET reduceOnly; spot:
one SELL OCO), then **leaves the protected position held** (status
``bracketed``). Broker I/O faked; ledger is the real service on the test
DB. The position is intentionally NOT flat at the end of this run.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo_scalping.contract import ScalpingRiskLimits
from app.services.brokers.binance.demo_scalping.order_intent import OrderIntent
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference

_NOW = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.UTC)
_REF = SymbolReference(
    price=Decimal("1.36"),
    step_size=Decimal("0.1"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.0001"),
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
        entry_reference_price=Decimal("1.36"),
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


class _Order:
    def __init__(self, status, coid, broker="b1"):
        self.status = status
        self.client_order_id = coid
        self.broker_order_id = broker
        self.executed_qty = Decimal("7.3")


class _OpenOrders:
    def __init__(self, orders):
        self.orders = orders


class _Oco:
    order_list_id = "777"
    list_status = "EXECUTING"
    leg_client_order_ids = ("tp-x", "sl-x")


class _Position:
    def __init__(self, amt):
        self.position_amt = amt
        self.is_flat = amt == 0
        self.entry_price = Decimal("1.36")
        self.leverage = 1


class _PosMode:
    is_hedge_mode = False


class _Lev:
    leverage = 1


class _FakeSpot:
    def __init__(self):
        self.submits = []
        self.ocos = []
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
        if side == "BUY":
            self._free = Decimal("7.3")
        return _Order("FILLED", client_order_id)

    async def submit_oco(
        self,
        *,
        symbol,
        side,
        quantity,
        tp_price,
        sl_stop_price,
        sl_limit_price,
        time_in_force="GTC",
        list_client_order_id=None,
        confirm=False,
    ):
        self.ocos.append(
            {
                "side": side,
                "qty": quantity,
                "tp": tp_price,
                "sl": sl_stop_price,
                "sl_limit": sl_limit_price,
                "confirm": confirm,
            }
        )
        return _Oco()

    async def get_asset_balance(self, *, asset):
        class _B:
            free = self._free

        b = _B()
        b.free = self._free
        return b

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


class _FakeFutures:
    def __init__(self, *, open_status="FILLED"):
        self.submits = []
        self.triggers = []
        self._amt = Decimal("0")
        self._open_status = open_status

    async def get_position_mode(self):
        return _PosMode()

    async def set_leverage(self, *, symbol, leverage):
        return _Lev()

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
        self.submits.append(side)
        self._amt = qty if side == "BUY" else -qty
        return _Order(self._open_status, client_order_id)

    async def submit_reduce_only_trigger(
        self,
        *,
        symbol,
        side,
        order_type,
        qty,
        stop_price,
        client_order_id=None,
        confirm=False,
    ):
        self.triggers.append(
            {
                "side": side,
                "type": order_type,
                "qty": qty,
                "stop_price": stop_price,
                "confirm": confirm,
            }
        )
        return _Order("NEW", client_order_id)

    async def get_order(self, *, symbol, client_order_id):
        return _Order("FILLED", client_order_id)

    async def get_position(self, *, symbol):
        return _Position(self._amt)

    async def get_open_orders(self, *, symbol):
        return _OpenOrders([])


async def _parent_row(db_session, open_cid):
    return await db_session.scalar(
        select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == open_cid
        )
    )


@pytest.mark.asyncio
async def test_spot_bracket_places_oco_and_holds(db_session) -> None:
    client = _FakeSpot()
    ex = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeRef(),
        now=_NOW,
        limits=_limits_for("EXEBRKSPOTUSDT"),
    )
    result = await ex.execute_bracket(_intent("spot", "EXEBRKSPOTUSDT"), confirm=True)
    assert result.status == "bracketed"
    assert list(client.submits) == ["BUY"]  # opened, NOT sold
    assert len(client.ocos) == 1 and client.ocos[0]["side"] == "SELL"
    row = await _parent_row(db_session, result.open_client_order_id)
    assert row.lifecycle_state == "filled"  # held, protected (not closed)
    assert "bracket" in (row.extra_metadata or {})


@pytest.mark.asyncio
async def test_futures_bracket_places_two_reduce_only_triggers(db_session) -> None:
    client = _FakeFutures(open_status="FILLED")
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_FakeRef(),
        now=_NOW,
        limits=_limits_for("EXEBRKFUTUSDT"),
    )
    result = await ex.execute_bracket(
        _intent("usdm_futures", "EXEBRKFUTUSDT"), confirm=True
    )
    assert result.status == "bracketed"
    types = sorted(t["type"] for t in client.triggers)
    assert types == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert all(t["side"] == "SELL" for t in client.triggers)  # close a long
    row = await _parent_row(db_session, result.open_client_order_id)
    assert row.lifecycle_state == "filled"


@pytest.mark.asyncio
async def test_bracket_dry_run_places_nothing(db_session) -> None:
    client = _FakeSpot()
    ex = DemoScalpingExecutor(
        product="spot",
        client=client,
        session=db_session,
        reference=_FakeRef(),
        now=_NOW,
        limits=_limits_for("EXEBRKDRYUSDT"),
    )
    result = await ex.execute_bracket(_intent("spot", "EXEBRKDRYUSDT"), confirm=False)
    assert result.status == "dry_run"
    assert client.submits == [] and client.ocos == []


@pytest.mark.asyncio
async def test_futures_bracket_unproven_fill_is_anomaly_no_bracket(db_session) -> None:
    # Open returns NEW and position stays flat -> fill unproven -> anomaly, no bracket.
    client = _FakeFutures(open_status="NEW")

    async def _flat(*, symbol):
        return _Position(Decimal("0"))

    client.get_position = _flat  # never goes non-flat
    client.get_order = lambda **k: _Order("NEW", k.get("client_order_id"))

    async def _get_order(*, symbol, client_order_id):
        return _Order("NEW", client_order_id)

    client.get_order = _get_order
    ex = DemoScalpingExecutor(
        product="usdm_futures",
        client=client,
        session=db_session,
        reference=_FakeRef(),
        now=_NOW,
        limits=_limits_for("EXEBRKANOMUSDT"),
        poll_max=2,
        poll_delay_seconds=0.0,
    )
    result = await ex.execute_bracket(
        _intent("usdm_futures", "EXEBRKANOMUSDT"), confirm=True
    )
    assert result.status == "anomaly"
    assert client.triggers == []  # no bracket on an unproven fill
