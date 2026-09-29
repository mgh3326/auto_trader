from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.brokers.binance.h5.holding import (
    Holding,
    choose_exit,
    entry_kill_reasons,
)
from app.services.brokers.binance.h5.sizing import H5SizingBlocked, size_entry


def _size(*, nav: str = "1000", price: str = "100", minimum: str = "5"):
    return size_entry(
        nav_usdt=Decimal(nav),
        executable_price=Decimal(price),
        step_size=Decimal("0.1"),
        min_notional_usdt=Decimal(minimum),
        min_qty=Decimal("0.1"),
        max_qty=Decimal("100"),
        quantity_precision=1,
    )


def test_h5_nav_risk_floor_and_min_notional() -> None:
    result = _size()
    assert result.qty == Decimal("2.0")
    assert result.notional_usdt == Decimal("200.0")
    assert result.maximum_notional_usdt == Decimal("200")
    floored = _size(price="103")
    assert floored.qty == Decimal("1.9")
    assert floored.notional_usdt == Decimal("195.7")
    blocked = False
    try:
        _size(nav="20", minimum="5")
    except H5SizingBlocked:
        blocked = True
    assert blocked is True


def _holding(**overrides) -> Holding:
    now = dt.datetime(2026, 9, 28, tzinfo=dt.UTC)
    data = {
        "side": "BUY",
        "entry_price": Decimal("100"),
        "entry_qty": Decimal("1"),
        "broker_remaining_qty": Decimal("1"),
        "closed_qty": Decimal("0"),
        "entered_at": now,
        "completed_bars_held": 0,
        "step_size": Decimal("0.1"),
    }
    data.update(overrides)
    return Holding(**data)


def test_stop_precedes_simultaneous_tp_and_time() -> None:
    now = dt.datetime(2026, 9, 29, tzinfo=dt.UTC)
    decision = choose_exit(
        _holding(completed_bars_held=6),
        quote_price=Decimal("105"),
        intrabar_low=Decimal("94"),
        intrabar_high=Decimal("106"),
        completed_bar_close=Decimal("97"),
        now=now,
    )
    assert decision is not None
    assert decision.reason == "hard_stop"
    assert decision.qty == Decimal("1")
    assert decision.reduce_only is True


def test_bar_stop_then_time_then_tp_priority() -> None:
    now = dt.datetime(2026, 9, 28, 4, tzinfo=dt.UTC)
    bar_stop = choose_exit(
        _holding(),
        quote_price=Decimal("103"),
        intrabar_low=Decimal("96"),
        intrabar_high=Decimal("104"),
        completed_bar_close=Decimal("97"),
        now=now,
    )
    assert bar_stop is not None and bar_stop.reason == "bar_close_stop"
    timed = choose_exit(
        _holding(completed_bars_held=6),
        quote_price=Decimal("105"),
        intrabar_low=Decimal("99"),
        intrabar_high=Decimal("106"),
        completed_bar_close=Decimal("100"),
        now=now,
    )
    assert timed is not None and timed.reason == "time_exit"


def test_partial_tp_uses_only_broker_proven_remainder() -> None:
    now = dt.datetime(2026, 9, 28, 1, tzinfo=dt.UTC)
    first = choose_exit(
        _holding(),
        quote_price=Decimal("103"),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=None,
        now=now,
    )
    assert first is not None and first.reason == "tp1" and first.qty == Decimal("0.5")
    residual = choose_exit(
        _holding(closed_qty=Decimal("0.3"), broker_remaining_qty=Decimal("0.7")),
        quote_price=Decimal("103"),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=None,
        now=now,
    )
    assert residual is not None and residual.qty == Decimal("0.2")
    final = choose_exit(
        _holding(closed_qty=Decimal("0.5"), broker_remaining_qty=Decimal("0.5")),
        quote_price=Decimal("105"),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=None,
        now=now,
    )
    assert final is not None and final.reason == "tp2" and final.qty == Decimal("0.5")
    assert final.reduce_only is True
    gap = choose_exit(
        _holding(),
        quote_price=Decimal("106"),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=None,
        now=now,
    )
    assert gap is not None and gap.reason == "tp2" and gap.qty == Decimal("1")


@pytest.mark.parametrize(
    "quote,bar_close,closed,expected",
    [
        ("97", None, "0", "tp1"),
        ("95", None, "0.5", "tp2"),
        ("98", "103", "0", "bar_close_stop"),
    ],
)
def test_short_exit_symmetry(quote, bar_close, closed, expected):
    decision = choose_exit(
        _holding(
            side="SELL",
            closed_qty=Decimal(closed),
            broker_remaining_qty=Decimal("1") - Decimal(closed),
        ),
        quote_price=Decimal(quote),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=Decimal(bar_close) if bar_close else None,
        now=dt.datetime(2026, 9, 28, 1, tzinfo=dt.UTC),
    )
    assert decision is not None and decision.reason == expected and decision.reduce_only


def test_wall_clock_exit_at_24h_without_six_complete_bars():
    decision = choose_exit(
        _holding(completed_bars_held=0),
        quote_price=Decimal("100"),
        intrabar_low=None,
        intrabar_high=None,
        completed_bar_close=None,
        now=dt.datetime(2026, 9, 29, tzinfo=dt.UTC),
    )
    assert decision is not None and decision.reason == "time_exit"


def test_short_hard_stop_and_entry_kills() -> None:
    now = dt.datetime(2026, 9, 28, 1, tzinfo=dt.UTC)
    stop = choose_exit(
        _holding(side="SELL"),
        quote_price=Decimal("104"),
        intrabar_low=Decimal("95"),
        intrabar_high=Decimal("105"),
        completed_bar_close=None,
        now=now,
    )
    assert stop is not None and stop.reason == "hard_stop"
    reasons = entry_kill_reasons(
        stop_losses_today=3,
        daily_pnl_usdt=Decimal("-30"),
        day_start_nav_usdt=Decimal("1000"),
        peak_nav_usdt=Decimal("1200"),
        current_nav_usdt=Decimal("1000"),
    )
    assert set(reasons) == {
        "three_stops_today",
        "daily_loss_entry_stop",
        "mdd_lane_stop",
    }
