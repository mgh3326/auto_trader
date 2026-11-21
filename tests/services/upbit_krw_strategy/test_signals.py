from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest

from app.mcp_server.tooling.market_data_indicators import _calculate_rsi_series
from app.services.upbit_krw_strategy import signals as s

from .support import LAST


def D(values):
    return tuple(Decimal(str(v)) for v in values)


def newest_first(oldest_first):
    return D(reversed(oldest_first))


# 61 closes, oldest first: rise 100..146, 13 falls of 2 (144..120), then a bounce.
PULLBACK = [100 + i for i in range(47)] + [146 - 2 * j for j in range(1, 14)] + [121]
STEADY_UP = [100 + i for i in range(61)]
NOISY_UP = [100 + (i * 3) // 2 * 1 for i in range(61)]  # replaced below


def noisy_up():
    out = [100]
    for i in range(60):
        out.append(out[-1] + (2 if i % 2 == 0 else -1))
    return out


def test_close_window_needs_all_61_consecutive_days() -> None:
    closes = {LAST - timedelta(days=k): Decimal(100 + k) for k in range(61)}
    window = s.close_window(closes, LAST)
    assert window is not None and len(window) == 61
    assert window[0] == Decimal(100) and window[60] == Decimal(160)  # C_0 newest first
    del closes[LAST - timedelta(days=30)]
    assert s.close_window(closes, LAST) is None  # no forward fill


def test_r60_golden() -> None:
    assert s.r60(newest_first(PULLBACK)) == Decimal("0.21")  # 121 / 100 - 1


def test_trend_enters_only_on_strictly_positive_r60() -> None:
    sig = s.evaluate_trend(newest_first(PULLBACK))
    assert (sig.family, sig.enter, sig.reason_code, sig.r60) == (
        "trend",
        True,
        "ok",
        Decimal("0.21"),
    )
    flat = s.evaluate_trend(D([100] * 61))
    assert (flat.enter, flat.reason_code, flat.r60) == (
        False,
        "r60_not_positive",
        Decimal(0),
    )
    down = s.evaluate_trend(newest_first([100 + 61 - i for i in range(61)]))
    assert not down.enter and down.reason_code == "r60_not_positive" and down.r60 < 0


def test_missing_or_invalid_close_is_no_signal() -> None:
    assert s.evaluate_trend(None).reason_code == "missing_close"
    assert s.evaluate_pullback(None).reason_code == "missing_close"
    assert s.evaluate_trend(newest_first(STEADY_UP)[:60]).reason_code == "missing_close"
    bad = list(D(STEADY_UP))
    bad[10] = Decimal(0)
    assert s.evaluate_trend(tuple(bad)).reason_code == "invalid_close"


def test_pullback_enters_when_oversold_trend_with_a_rebound() -> None:
    window = newest_first(PULLBACK)
    sig = s.evaluate_pullback(window)
    assert sig.enter and sig.reason_code == "ok" and sig.r60 == Decimal("0.21")
    assert sig.rsi14 is not None and sig.rsi14 <= Decimal(40)


def test_pullback_reasons() -> None:
    assert s.evaluate_pullback(D([100] * 61)).reason_code == "r60_not_positive"
    # steady rise: never a loss, so the RSI is undefined (same NaN as ROB-1382)
    assert s.evaluate_pullback(newest_first(STEADY_UP)).reason_code == "rsi_unavailable"
    above = s.evaluate_pullback(newest_first(noisy_up()))
    assert above.reason_code == "rsi_above_max" and above.rsi14 > Decimal(40)
    falling_on = PULLBACK[:-1] + [119]  # last day keeps falling: C_0 <= C_1
    no_rebound = s.evaluate_pullback(newest_first(falling_on))
    assert no_rebound.reason_code == "no_rebound" and no_rebound.rsi14 <= Decimal(40)
    equal = s.evaluate_pullback(newest_first(PULLBACK[:-1] + [120]))
    assert equal.reason_code == "no_rebound"  # C_0 == C_1 is not a rebound


@pytest.mark.parametrize(
    ("rsi", "enter"), [("40", True), ("40.0000001", False), ("12.5", True)]
)
def test_pullback_rsi_threshold_is_inclusive(monkeypatch, rsi, enter) -> None:
    monkeypatch.setattr(s, "rsi14", lambda _closes: Decimal(rsi))
    assert s.evaluate_pullback(newest_first(PULLBACK)).enter is enter


def test_pullback_signal_set_is_a_subset_of_trend() -> None:
    for series in (PULLBACK, STEADY_UP, noisy_up(), [100] * 61, PULLBACK[:-1] + [119]):
        window = newest_first(series)
        if s.evaluate_pullback(window).enter:
            assert s.evaluate_trend(window).enter


def test_rsi_none_cases_and_extremes() -> None:
    assert s.rsi14(D(range(1, 14))) is None  # 13 closes: below min_periods 14
    assert s.rsi14(D(range(1, 15))) is None  # 14 closes, but no loss at all
    assert s.rsi14(D([5] * 15)) is None  # flat
    assert s.rsi14(D(range(15, 0, -1))) == Decimal(0)  # no gain
    assert (
        s.rsi14(D([3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2, 3, 2])) is not None
    )  # 14 closes


def _pandas_rsi(closes) -> float:
    series = _calculate_rsi_series(pd.Series([float(c) for c in closes]))
    return float(series.iloc[-1])


@pytest.mark.parametrize("seed", range(12))
def test_rsi_matches_the_single_pandas_implementation(seed: int) -> None:
    state = seed * 7919 + 13
    closes = [Decimal(1000)]
    for _ in range(60):
        state = (state * 1103515245 + 12345) % (2**31)
        step = Decimal((state % 2001) - 1000) / Decimal(100)  # -10.00 .. +10.00
        closes.append(max(Decimal(1), closes[-1] + step))
    mine = s.rsi14(tuple(closes))
    assert mine is not None
    assert abs(float(mine) - _pandas_rsi(closes)) < 1e-9


def test_rsi_over_the_real_window_matches_pandas() -> None:
    mine = s.rsi14(D(PULLBACK))
    assert mine is not None
    assert abs(float(mine) - _pandas_rsi(D(PULLBACK))) < 1e-9


def test_rsi_with_exactly_14_closes_matches_pandas() -> None:
    closes = D([10, 12, 11, 13, 12, 14, 13, 15, 14, 13, 12, 13, 11, 12])
    mine = s.rsi14(closes)
    assert mine is not None
    assert abs(float(mine) - _pandas_rsi(closes)) < 1e-9
