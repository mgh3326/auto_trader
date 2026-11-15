"""TrendStrategy signal, volatility estimate and sizing; pure, no I/O."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.services.brokers.binance.demo_strategy_loop.strategy import StrategyPlugin
from app.services.brokers.binance.trend.constants import IDENTITY, SIGMA_STAR
from app.services.brokers.binance.trend.sizing import (
    TrendSizingBlocked,
    size_entry,
    target_notional,
)
from app.services.brokers.binance.trend.strategy import (
    DAY_MS,
    TrendBar4h,
    TrendStrategy,
    evaluate_all,
    momentum_sign,
    realized_vol_annual,
    required_close_times,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS

pytestmark = pytest.mark.unit

T = 10_000 * DAY_MS  # a 4h-aligned decision time
STOP = D("0.05")


def bar(close_ts: int, close: str) -> TrendBar4h:
    price = float(close)
    return TrendBar4h(
        ts=close_ts - FOUR_HOUR_MS,
        close_ts=close_ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1.0,
        is_segment_start=True,
        close_text=close,
    )


def series(decision_ts: int, closes_by_day_back: dict[int, str]) -> tuple:
    return tuple(
        bar(decision_ts - k * DAY_MS, c)
        for k, c in sorted(closes_by_day_back.items(), reverse=True)
    )


def full(decision_ts: int, *, lookback: int, now: str, past: str, daily="100"):
    """Daily closes of ``daily`` except t (``now``) and t-L (``past``)."""
    closes = dict.fromkeys(range(0, 21), daily)
    closes[lookback] = past
    closes[0] = now
    return series(decision_ts, closes)


def test_lookback_and_stop_are_required_with_no_defaults() -> None:
    with pytest.raises(TypeError):
        TrendStrategy()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TrendStrategy(lookback_days=20)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        TrendStrategy(hard_stop_fraction=STOP)  # type: ignore[call-arg]


@pytest.mark.parametrize("lookback", [0, 10, 21, 90, 119, True, 20.0])
def test_lookback_must_be_one_of_the_registered_choices(lookback) -> None:
    with pytest.raises(ValueError, match="lookback_days"):
        TrendStrategy(lookback_days=lookback, hard_stop_fraction=STOP)


@pytest.mark.parametrize("stop", [D("0"), D("1"), D("-0.1"), D("1.5"), D("NaN"), 0.05])
def test_hard_stop_fraction_must_be_a_decimal_strictly_inside_zero_one(stop) -> None:
    with pytest.raises(ValueError, match="hard_stop_fraction"):
        TrendStrategy(lookback_days=20, hard_stop_fraction=stop)


@pytest.mark.parametrize("lookback", [20, 60, 120])
def test_the_registered_lookbacks_construct_and_follow_the_plugin_protocol(
    lookback,
) -> None:
    plugin = TrendStrategy(lookback_days=lookback, hard_stop_fraction=STOP)
    assert plugin.strategy_id == IDENTITY == "TREND-TSMOM-v1"
    assert isinstance(plugin, TrendStrategy)
    assert StrategyPlugin in TrendStrategy.__mro__


def test_identity_cannot_be_changed() -> None:
    with pytest.raises(ValueError, match="identity"):
        TrendStrategy(lookback_days=20, hard_stop_fraction=STOP, strategy_id="x")


@pytest.mark.parametrize(
    ("now", "past", "side"),
    [("101", "100", "BUY"), ("99", "100", "SELL"), ("100", "100", None)],
)
def test_signal_is_the_sign_of_the_lookback_return(now, past, side) -> None:
    bars = full(T, lookback=60, now=now, past=past)
    assert momentum_sign(bars, decision_ts=T, lookback_days=60) == side


def test_the_past_bar_is_exactly_l_days_back_not_the_nearest_one() -> None:
    bars = full(T, lookback=20, now="101", past="100")
    shifted = tuple(b for b in bars if b.close_ts != T - 20 * DAY_MS) + (
        bar(T - 20 * DAY_MS - FOUR_HOUR_MS, "100"),
    )
    assert momentum_sign(shifted, decision_ts=T, lookback_days=20) is None


@pytest.mark.parametrize("missing_day", [0, 20, 7])
def test_a_missing_bar_is_no_signal_never_a_substitute(missing_day: int) -> None:
    bars = tuple(
        b
        for b in full(T, lookback=20, now="101", past="100")
        if b.close_ts != T - missing_day * DAY_MS
    )
    plugin = TrendStrategy(lookback_days=20, hard_stop_fraction=STOP)
    assert plugin.evaluate_symbol("BTCUSDT", bars, decision_ts=T) is None


def test_a_signal_needs_the_full_volatility_window_to_be_sizeable() -> None:
    plugin = TrendStrategy(lookback_days=60, hard_stop_fraction=STOP)
    bars = full(T, lookback=60, now="101", past="100", daily="100")
    # daily closes are all 100 except t: realized vol is positive, so a signal.
    assert plugin.evaluate_symbol("BTCUSDT", bars, decision_ts=T) is not None
    flat = full(T, lookback=60, now="100", past="99", daily="100")
    assert realized_vol_annual(flat, decision_ts=T) is None
    assert plugin.evaluate_symbol("BTCUSDT", flat, decision_ts=T) is None


def test_evaluate_returns_the_first_universe_symbol_and_evaluate_all_every_one() -> (
    None
):
    plugin = TrendStrategy(lookback_days=20, hard_stop_fraction=STOP)
    up = full(T, lookback=20, now="101", past="100")
    down = full(T, lookback=20, now="99", past="100")
    bars = {"ETHUSDT": up, "XRPUSDT": down, "DOGEUSDT": up}
    first = plugin.evaluate(bars, decision_ts=T)
    assert (first.symbol, first.side) == ("ETHUSDT", "BUY")
    everything = evaluate_all(plugin, bars, decision_ts=T)
    # DOGEUSDT is outside the locked universe and never signals.
    assert [(s.symbol, s.side) for s in everything] == [
        ("ETHUSDT", "BUY"),
        ("XRPUSDT", "SELL"),
    ]
    assert {s.strategy_id for s in everything} == {IDENTITY}


def test_required_close_times_stay_bounded_even_for_the_longest_lookback() -> None:
    times = required_close_times(T, 120)
    assert len(times) == 22  # 21 daily samples + t-120d, not ~720 bars
    assert T in times and T - 120 * DAY_MS in times
    assert len(required_close_times(T, 20)) == 21


def test_hard_stop_price_is_a_fraction_of_the_entry_on_the_losing_side() -> None:
    plugin = TrendStrategy(lookback_days=20, hard_stop_fraction=STOP)
    assert plugin.hard_stop_price(side="BUY", entry_price=D("100")) == D("95.00")
    assert plugin.hard_stop_price(side="SELL", entry_price=D("100")) == D("105.00")


# ---------------------------------------------------------------- sizing


def test_notional_scales_down_with_volatility_and_never_above_the_cap() -> None:
    cap = D("60")
    assert target_notional(D("0.30")) == cap  # calm: min(1, 0.6/0.3) -> cap
    assert target_notional(SIGMA_STAR) == cap
    assert target_notional(D("1.20")) == cap / 2  # wild: scaled down
    assert target_notional(D("0.0001")) == cap  # no scale-up above the cap
    for bad in (D("0"), D("-1"), D("NaN")):
        with pytest.raises(TrendSizingBlocked):
            target_notional(bad)


def _size(**changes):
    args = {
        "sigma_hat": D("1.2"),
        "executable_price": D("100"),
        "step_size": D("0.001"),
        "min_notional_usdt": D("5"),
        "min_qty": D("0.001"),
        "max_qty": D("1000"),
        "quantity_precision": 3,
    }
    args.update(changes)
    return size_entry(**args)


def test_size_floors_to_the_step_and_reserves_at_least_the_exposure() -> None:
    size = _size()
    assert size.qty == D("0.300")  # 30 USDT / 100
    assert size.reserved_notional_usdt >= size.qty * D("100")
    assert size.reserved_notional_usdt <= D("60")
    odd = _size(executable_price=D("33.3333"), step_size=D("0.01"))
    assert odd.reserved_notional_usdt >= odd.qty * D("33.3333")
    assert odd.qty * D("33.3333") <= odd.target_notional_usdt


def test_sizing_never_rounds_up_to_reach_a_minimum() -> None:
    with pytest.raises(TrendSizingBlocked, match="minimums"):
        _size(min_notional_usdt=D("31"))  # 30 USDT target cannot reach 31
    with pytest.raises(TrendSizingBlocked, match="minimums"):
        _size(min_qty=D("0.5"))


@pytest.mark.parametrize(
    "change",
    [
        {"executable_price": D("0")},
        {"step_size": D("NaN")},
        {"quantity_precision": 13},
        {"min_qty": D("2"), "max_qty": D("1")},
    ],
)
def test_sizing_refuses_invalid_exchange_inputs(change) -> None:
    with pytest.raises(TrendSizingBlocked):
        _size(**change)


def test_a_quantity_the_step_floor_pushes_off_the_precision_grid_is_refused() -> None:
    # sigma 1.2 -> 30 USDT target at price 700: 0.042 -> 0.04 (2 dp) -> 0.039 (step)
    with pytest.raises(TrendSizingBlocked, match="representable"):
        _size(
            executable_price=D("700"),
            step_size=D("0.003"),
            quantity_precision=2,
            min_notional_usdt=D("5"),
            min_qty=D("0.003"),
            max_qty=D("1000"),
        )
    # a step that is a multiple of the precision increment still sizes
    assert _size(
        executable_price=D("700"), step_size=D("0.01"), quantity_precision=2
    ).qty == D("0.04")
