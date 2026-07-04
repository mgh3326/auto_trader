"""ROB-703 — PaperLimitOrderService place/reconcile/cancel/list integration tests.

Heavy integration tests against the shared ``db_session`` fixture (Postgres
``public`` schemas pre-built via ``Base.metadata.create_all``). OHLCV fetch
is monkeypatched to return canned bars, so these tests do not require live
Upbit connectivity.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.core.timezone import now_kst
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService


def _candle(low: Decimal, high: Decimal, timestamp: dt.datetime | None = None) -> Any:
    class _C:
        pass

    c = _C()
    c.low = low
    c.high = high
    c.timestamp = timestamp
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


@pytest.mark.asyncio
async def test_reconcile_handles_tz_naive_upbit_timestamps(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Upbit candles carry tz-NAIVE timestamps; placed_at is tz-aware.
    Reconcile must not raise TypeError comparing them (blocker #1)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-tz"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    # candle AFTER placement, tz-NAIVE (as real Upbit candle_date_time_kst is), low crosses
    naive_after = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("89000000"), Decimal("91000000"), naive_after)]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 1, res  # must fill, not crash


@pytest.mark.asyncio
async def test_reconcile_excludes_bars_before_placement(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A limit must not fill on price action BEFORE it was placed (no look-ahead)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-pre"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    # only a crossing candle BEFORE placement exists -> must stay pending
    naive_before = now_kst().replace(tzinfo=None) - dt.timedelta(minutes=5)

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("89000000"), Decimal("91000000"), naive_before)]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    assert res["filled"] == 0, res
    assert len(await svc.list_pending_orders(account_id=acct.id)) == 1


@pytest.mark.asyncio
async def test_reconcile_isolates_failure_no_double_fill(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing order in the batch must not abort the batch nor leave an
    already-booked trade re-fillable on the next reconcile (blocker #2)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-iso"), initial_capital_krw=Decimal("1000000")
    )
    acct_id = acct.id
    svc = PaperLimitOrderService(db_session)
    # Order A: buy that will cross and fill fine.
    await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )
    ts = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("89000000"), Decimal("91000000"), ts)]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    # Directly insert a crossing SELL pending order with no position -> execute_order raises.
    from app.models.paper_trading import PaperPendingOrder

    bad = PaperPendingOrder(
        account_id=acct_id,
        symbol="KRW-ETH",
        side="sell",
        order_type="limit",
        limit_price=Decimal("1000000"),
        quantity=Decimal("0.1"),
        reserved_krw=Decimal("0"),
        status="pending",
        placed_at=now_kst(),
    )
    db_session.add(bad)
    await db_session.commit()

    res = await svc.reconcile_pending_orders(account_id=acct_id, now=None)
    assert res["filled"] == 1, res  # A filled; the bad sell did not abort the batch

    # A must now be 'filled' and NOT re-fillable.
    trades1 = await pts.get_trade_history(acct_id, limit=50)
    btc_trades1 = [t for t in trades1 if t["symbol"] == "KRW-BTC"]
    assert len(btc_trades1) == 1, btc_trades1
    await svc.reconcile_pending_orders(account_id=acct_id, now=None)
    trades2 = await pts.get_trade_history(acct_id, limit=50)
    btc_trades2 = [t for t in trades2 if t["symbol"] == "KRW-BTC"]
    assert len(btc_trades2) == 1, f"double-fill: {btc_trades2}"


@pytest.mark.asyncio
async def test_place_sell_reserves_position_across_pending(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two resting sells cannot jointly exceed the held position (major #3)."""
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-sell"), initial_capital_krw=Decimal("100000000")
    )
    # establish a 0.002 BTC position via a market buy

    async def _price(symbol, itype):  # noqa: ARG001
        return Decimal("50000000")

    monkeypatch.setattr(pts, "_fetch_current_price", _price)
    await pts.execute_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        quantity=Decimal("0.002"),
    )
    svc = PaperLimitOrderService(db_session)
    first = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="sell",
        limit_price=Decimal("60000000"),
        quantity=Decimal("0.002"),
    )
    assert first["success"], first
    second = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="sell",
        limit_price=Decimal("61000000"),
        quantity=Decimal("0.002"),
    )
    assert not second["success"]
    assert (
        "sellable" in second["error"].lower()
        or "insufficient" in second["error"].lower()
    )


@pytest.mark.asyncio
async def test_place_sell_below_min_notional_rejected(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-min"), initial_capital_krw=Decimal("100000000")
    )

    async def _price(symbol, itype):  # noqa: ARG001
        return Decimal("50000000")

    monkeypatch.setattr(pts, "_fetch_current_price", _price)
    await pts.execute_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        quantity=Decimal("0.01"),
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="sell",
        limit_price=Decimal("50000000"),
        quantity=Decimal("0.00001"),  # 500 KRW
    )
    assert not out["success"]
    assert "minimum" in out["error"].lower()


@pytest.mark.asyncio
async def test_reconcile_failure_does_not_skip_later_orders(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing order EARLY in the batch must not skip a later crossed order in
    the SAME reconcile pass. Pre-fix, the except-branch rollback expired the
    pre-loaded ORM list so every later order raised MissingGreenlet and was
    silently skipped; the id-based loop fixes it (regression guard for fix A)."""
    from app.models.paper_trading import PaperPendingOrder

    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob703-tail"), initial_capital_krw=Decimal("1000000")
    )
    # Bad SELL (no position) inserted FIRST -> lower placed_at, processed first,
    # raises inside execute_order -> triggers the per-order rollback.
    bad = PaperPendingOrder(
        account_id=acct.id,
        symbol="KRW-ETH",
        side="sell",
        order_type="limit",
        limit_price=Decimal("1000000"),
        quantity=Decimal("0.1"),
        reserved_krw=Decimal("0"),
        status="pending",
        placed_at=now_kst() - dt.timedelta(minutes=1),
    )
    db_session.add(bad)
    await db_session.commit()

    svc = PaperLimitOrderService(db_session)
    # Good BUY placed AFTER -> later placed_at, processed second, must still fill.
    await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
    )

    ts = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)

    async def _bars(symbol: str, market: str, period: str, count: int, end: Any = None) -> Any:
        if symbol == "KRW-ETH":  # sell limit 1_000_000 crossed by high 2_000_000
            return [_candle(Decimal("500000"), Decimal("2000000"), ts)]
        return [_candle(Decimal("89000000"), Decimal("91000000"), ts)]  # buy crossed

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)

    res = await svc.reconcile_pending_orders(account_id=acct.id, now=None)
    # bad sell fails, but the later good buy MUST still fill in this same pass
    assert res["filled"] == 1, res
    pending = await svc.list_pending_orders(account_id=acct.id)
    assert not any(o["symbol"] == "KRW-BTC" for o in pending), (
        "the good buy must be filled, not left pending, after an earlier failure"
    )
