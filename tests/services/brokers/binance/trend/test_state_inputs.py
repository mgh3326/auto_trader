"""Service input validation refuses before any database use (no DB)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from app.services.brokers.binance.trend.state import (
    TrendStateBlocked,
    TrendStateService,
)

pytestmark = pytest.mark.asyncio

NOW = dt.datetime(2026, 11, 14, 3, tzinfo=dt.UTC)
NAIVE = dt.datetime(2026, 11, 14, 3)


def _service() -> TrendStateService:
    def factory():
        raise AssertionError("the database must not be touched")

    return TrendStateService(factory)


@pytest.mark.parametrize(
    ("symbol", "side", "price"),
    [
        ("DOGEUSDT", "BUY", "1"),
        ("BTCUSDT", "HOLD", "1"),
        ("BTCUSDT", "BUY", "0"),
        ("BTCUSDT", "BUY", "x"),
    ],
)
async def test_observe_refuses_non_trend_signals(
    symbol: str, side: str, price: str
) -> None:
    with pytest.raises(TrendStateBlocked):
        await _service().observe_signal(
            symbol=symbol, side=side, decision_ts=1_700_000_000_000, price_text=price
        )


async def test_naive_timestamps_are_refused() -> None:
    svc = _service()
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.reserve_entry(signal_key="k", notional_usdt=D("60"), now=NAIVE)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.mark_sending("c", now=NAIVE)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.mark_uncertain("c", now=NAIVE)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.block_unsent("k", now=NAIVE)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.record_conflict_blocked("k", now=NAIVE)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.record_conflict_rejected("c", broker_status="x", now=NAIVE)


@pytest.mark.parametrize(
    "change",
    [
        {"qty": D("0")},
        {"qty": D("-1")},
        {"qty": D("NaN")},
        {"side": "HOLD"},
        {"reduce_only": True},
        {"leg_key": "exit"},
        {"reference_price": D("0")},
        {"reference_price": D("NaN")},
    ],
)
async def test_reserve_intent_only_takes_a_valid_entry_leg(change: dict) -> None:
    args = {
        "signal_key": "k",
        "leg_key": "entry",
        "side": "BUY",
        "qty": D("1"),
        "reference_price": D("10"),
        "reduce_only": False,
        "now": NOW,
    } | change
    with pytest.raises(TrendStateBlocked):
        await _service().reserve_intent(**args)


async def test_resolve_conflicts_needs_an_identity() -> None:
    svc = _service()
    for who in ("", "  ", "x" * 101):
        with pytest.raises(TrendStateBlocked, match="resolver"):
            await svc.resolve_conflicts(resolved_by=who, now=NOW)
    with pytest.raises(TrendStateBlocked, match="aware"):
        await svc.resolve_conflicts(resolved_by="op", now=NAIVE)


async def test_conflict_detail_is_bounded_and_status_is_required() -> None:
    svc = _service()
    with pytest.raises(TrendStateBlocked, match="too long"):
        await svc.record_conflict_blocked("k", now=NOW, detail="x" * 201)
    with pytest.raises(TrendStateBlocked, match="status required"):
        await svc.record_conflict_rejected("c", broker_status="", now=NOW)
