"""ROB-703 — PaperLimitOrderService place/reconcile/cancel/list integration tests.

Heavy integration tests against the shared ``db_session`` fixture (Postgres
``public`` schemas pre-built via ``Base.metadata.create_all``). OHLCV fetch
is monkeypatched to return canned bars, so these tests do not require live
Upbit connectivity.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService


def _candle(low: Decimal, high: Decimal) -> Any:
    class _C:
        pass

    c = _C()
    c.low = low
    c.high = high
    c.timestamp = None
    return c


def _uniq(prefix: str) -> str:
    """Per-test unique account name (shared ``db_session`` does not rollback)."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_place_limit_buy_rests_and_reserves_cash(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-a"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    assert out["success"], out
    assert out["status"] == "pending"
    assert out["reserved_krw"] > Decimal("0")
    cash = await pts.get_cash_balance(acct.id)
    assert cash["krw"] < Decimal("1000000"), (
        "placing a resting buy must reserve cash against cash_krw"
    )


@pytest.mark.asyncio
async def test_place_limit_rejects_inactive_account(
    db_session: Any,
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-inactive"), initial_capital_krw=Decimal("1000000")
    )
    acct.is_active = False
    await db_session.commit()
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    assert not out["success"]
    assert "inactive" in out["error"].lower()


@pytest.mark.asyncio
async def test_place_limit_rejects_below_min_notional(
    db_session: Any,
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-min"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("1000"),  # < 5000 KRW Upbit minimum
    )
    assert not out["success"]
    assert "minimum" in out["error"].lower()


@pytest.mark.asyncio
async def test_reconcile_fills_when_market_crosses(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-b"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    place = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    order_id = place["order_id"]

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("89000000"), Decimal("91000000"))]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id)
    assert res["success"]
    assert res["filled"] == 1
    assert res["reconciled"] == 1

    pending = await svc.list_pending_orders(account_id=acct.id)
    assert pending == [], "filled order must drop out of pending listing"

    detail = await svc.get_pending_order(account_id=acct.id, order_id=order_id)
    assert detail["status"] == "filled"
    assert detail["fill_price"] == Decimal("90000000")
    assert detail["paper_trade_id"] is not None

    positions = await pts.get_positions(acct.id)
    assert any(p["symbol"] == "KRW-BTC" for p in positions), (
        "filled resting buy must produce an open PaperPosition"
    )


@pytest.mark.asyncio
async def test_reconcile_no_cross_stays_pending(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-c"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("50000000"),  # far below current bar
        amount=Decimal("100000"),
    )

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("89000000"), Decimal("91000000"))]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id)
    assert res["filled"] == 0
    pending = await svc.list_pending_orders(account_id=acct.id)
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_cancel_pending_order_releases_reservation(
    db_session: Any,
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-d"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    place = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    cash_before_cancel = (await pts.get_cash_balance(acct.id))["krw"]
    out = await svc.cancel_pending_order(account_id=acct.id, order_id=place["order_id"])
    assert out["success"]
    assert out["status"] == "cancelled"
    cash_after_cancel = (await pts.get_cash_balance(acct.id))["krw"]
    assert cash_after_cancel > cash_before_cancel, (
        "cancel must release reserved_krw back to cash_krw"
    )


@pytest.mark.asyncio
async def test_cancel_missing_order_returns_error(
    db_session: Any,
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-missing"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.cancel_pending_order(account_id=acct.id, order_id=99999999)
    assert not out["success"]
    assert "not found" in out["error"].lower()


@pytest.mark.asyncio
async def test_reconcile_sell_fills_when_high_touches(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Buy limit then reconcile a sell limit at a higher price — both fill."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-e"), initial_capital_krw=Decimal("5000000")
    )
    svc = PaperLimitOrderService(db_session)

    buy = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("200000"),  # buys ~0.002 BTC
    )
    assert buy["success"]

    async def _bars_cross(symbol, market, period, count, end=None):  # noqa: ARG001
        # dips enough to trigger the buy
        return [_candle(Decimal("89000000"), Decimal("91000000"))]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars_cross)
    await svc.reconcile_pending_orders(account_id=acct.id)
    positions = await pts.get_positions(acct.id)
    btc_pos = next(p for p in positions if p["symbol"] == "KRW-BTC")
    sell = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="sell",
        limit_price=Decimal("100000000"),
        quantity=btc_pos["quantity"],
    )
    assert sell["success"]

    async def _bars_high(symbol, market, period, count, end=None):  # noqa: ARG001
        # rises enough to trigger the sell
        return [_candle(Decimal("98000000"), Decimal("101000000"))]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars_high)
    res = await svc.reconcile_pending_orders(account_id=acct.id)
    assert res["filled"] == 1
    detail = await svc.get_pending_order(account_id=acct.id, order_id=sell["order_id"])
    assert detail["status"] == "filled"
    assert detail["fill_price"] == Decimal("100000000")
