"""ROB-705 — Place-time provenance + fill bridge integration tests.

Verifies ``place_limit_order`` stamps the deterministic ``correlation_id``
spine on the ``PaperPendingOrder``, links a draft ``TradeJournal``, and (when
probability/target/review-date are supplied) creates a ``price_target``
Forecast carrying the same correlation id. Also verifies ``reconcile_pending_orders``
carries the spine onto the booked ``PaperTrade`` and activates the draft journal.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.timezone import now_kst
from app.models.paper_trading import PaperPendingOrder, PaperTrade
from app.services.paper_limit_order_service import PaperLimitOrderService
from app.services.paper_trading_service import PaperTradingService


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_place_stamps_correlation_and_journal(db_session: Any) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-prov"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
        thesis="support bounce",
        strategy="support_ladder",
        target_price=Decimal("100000000"),
        stop_loss=Decimal("85000000"),
        probability=0.6,
        review_date="2026-07-15",
    )
    assert out["success"], out
    row = (
        await db_session.execute(
            select(PaperPendingOrder).where(PaperPendingOrder.id == out["order_id"])
        )
    ).scalar_one()
    assert row.correlation_id and row.correlation_id.startswith(f"paper:{acct.id}:")
    assert row.journal_id is not None  # draft journal linked
    assert row.forecast_id is not None  # forecast linked


def _candle(low: Decimal, high: Decimal, timestamp: dt.datetime | None = None) -> Any:
    class _C:
        pass

    c = _C()
    c.low = low
    c.high = high
    c.timestamp = timestamp
    return c


@pytest.mark.asyncio
async def test_fill_carries_correlation_to_paper_trade(
    db_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-fill"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    place = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("94400000"),
        amount=Decimal("100000"),
        thesis="support bounce",
    )
    assert place["success"], place
    corr_id = place["correlation_id"]
    assert corr_id and corr_id.startswith(f"paper:{acct.id}:")

    ts = now_kst().replace(tzinfo=None) + dt.timedelta(minutes=1)

    async def _bars(symbol, market, period, count, end=None):  # noqa: ARG001
        return [_candle(Decimal("94000000"), Decimal("95000000"), ts)]

    monkeypatch.setattr("app.services.paper_limit_order_service.get_ohlcv", _bars)
    res = await svc.reconcile_pending_orders(account_id=acct.id)
    assert res["success"] and res["filled"] == 1, res

    tr = (
        await db_session.execute(
            select(PaperTrade).where(PaperTrade.account_id == acct.id)
        )
    ).scalar_one()
    assert tr.correlation_id == corr_id
    assert tr.journal_id is not None


@pytest.mark.asyncio
async def test_buy_forecast_direction_is_at_or_above(db_session: Any) -> None:
    """A paper BUY forecasts price RISING to its profit target -> at_or_above.
    at_or_below is trivially true from bar 1 (target sits above entry) and would
    poison the Brier calibration signal for every paper buy (regression guard)."""
    from sqlalchemy import text

    pts = PaperTradingService(db_session)
    acct = await pts.create_account(
        name=_uniq("rob705-dir"), initial_capital_krw=Decimal("1000000")
    )
    svc = PaperLimitOrderService(db_session)
    out = await svc.place_limit_order(
        account_id=acct.id,
        symbol="KRW-BTC",
        side="buy",
        limit_price=Decimal("90000000"),
        amount=Decimal("100000"),
        thesis="support bounce",
        target_price=Decimal("110000000"),
        probability=0.5,
        review_date="2026-07-20",
    )
    assert out["success"] and out["forecast_id"], out
    row = (
        await db_session.execute(
            text(
                "SELECT forecast_target->>'direction' FROM review.trade_forecasts "
                "WHERE correlation_id = :c"
            ),
            {"c": out["correlation_id"]},
        )
    ).first()
    assert row is not None, "forecast row not found by correlation_id"
    assert row[0] == "at_or_above", (
        f"buy profit-target forecast must be at_or_above, got {row[0]!r}"
    )
