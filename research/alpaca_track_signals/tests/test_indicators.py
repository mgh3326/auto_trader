from __future__ import annotations

import math

import daily_bars as db
import indicators as ind
import pytest


def _bar(day_index: int, close: float, *, is_valid=True, is_segment_start=False):
    day_start = day_index * db.DAY_MS
    return db.DailyBar(
        day_start_ms=day_start,
        day_end_ms=day_start + db.DAY_MS,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=0.0,
        minute_count_observed=1440,
        imputed_minutes=0,
        max_gap_minutes=0,
        gap_in_last_60min=False,
        is_valid=is_valid,
        is_segment_start=is_segment_start,
    )


# --------------------------------------------------------------------------- #
# ema_final_value
# --------------------------------------------------------------------------- #


def test_ema_final_value_with_exactly_period_closes_is_the_sma_seed():
    closes = [10.0, 20.0, 30.0]
    assert ind.ema_final_value(closes, 3) == pytest.approx(20.0)


def test_ema_final_value_updates_recursively_after_the_seed():
    closes = [10.0, 20.0, 30.0, 40.0]
    seed = (10.0 + 20.0 + 30.0) / 3
    alpha = 2.0 / 4
    expected = alpha * 40.0 + (1 - alpha) * seed
    assert ind.ema_final_value(closes, 3) == pytest.approx(expected)


def test_ema_final_value_raises_when_fewer_than_period_closes():
    with pytest.raises(ind.InsufficientPriceHistoryError):
        ind.ema_final_value([1.0, 2.0], 3)


# --------------------------------------------------------------------------- #
# compute_trend_d / compute_momentum_r
# --------------------------------------------------------------------------- #


def test_compute_trend_d_is_fast_ema_over_slow_ema_minus_one():
    closes = [100.0 + i for i in range(90)]
    d = ind.compute_trend_d(closes, f=14, s=56)
    ema_f = ind.ema_final_value(closes, 14)
    ema_s = ind.ema_final_value(closes, 56)
    assert d == pytest.approx(ema_f / ema_s - 1.0)


def test_compute_momentum_r_boundary_needs_m_plus_one_closes():
    closes_ok = [1.0] * 29  # m=28 -> need 29
    assert ind.compute_momentum_r(closes_ok, m=28) == pytest.approx(0.0)
    with pytest.raises(ind.InsufficientPriceHistoryError):
        ind.compute_momentum_r([1.0] * 28, m=28)


def test_compute_momentum_r_matches_the_literal_formula():
    closes = [50.0] * 10 + [55.0]  # c_t=55, c_t-5 = 50.0
    r = ind.compute_momentum_r(closes, m=5)
    assert r == pytest.approx(55.0 / 50.0 - 1.0)


def test_compute_score_is_the_same_shape_as_momentum_r():
    closes = [50.0] * 15 + [60.0]
    assert ind.compute_score(closes, ell=14) == ind.compute_momentum_r(closes, m=14)


# --------------------------------------------------------------------------- #
# annualized_sigma20
# --------------------------------------------------------------------------- #


def test_annualized_sigma20_boundary_needs_21_closes():
    closes_20 = [100.0 * (1.01**i) for i in range(20)]
    with pytest.raises(ind.SigmaInsufficientSampleError):
        ind.annualized_sigma20(closes_20)
    closes_21 = [100.0 * (1.01**i) for i in range(21)]
    sigma = ind.annualized_sigma20(closes_21)
    assert sigma > 0.0
    assert math.isfinite(sigma)


def test_annualized_sigma20_matches_manual_sample_stdev_times_sqrt_365():
    closes = [100.0, 101.0, 99.0, 102.0, 98.0] * 5  # 25 closes, use last 21
    tail = closes[-21:]
    log_returns = [math.log(tail[i] / tail[i - 1]) for i in range(1, 21)]
    mean = sum(log_returns) / 20
    variance = sum((x - mean) ** 2 for x in log_returns) / 19
    expected = math.sqrt(variance) * math.sqrt(365)
    assert ind.annualized_sigma20(closes) == pytest.approx(expected)


def test_annualized_sigma20_rejects_degenerate_zero_variance_series():
    closes = [100.0] * 21
    with pytest.raises(ind.SigmaInsufficientSampleError):
        ind.annualized_sigma20(closes)


# --------------------------------------------------------------------------- #
# trailing_valid_segment
# --------------------------------------------------------------------------- #


def test_trailing_valid_segment_stops_at_the_nearest_segment_start():
    bars = [
        _bar(0, 10.0, is_segment_start=True),
        _bar(1, 11.0),
        _bar(2, 12.0, is_segment_start=True),  # a gap-restart happened here
        _bar(3, 13.0),
    ]
    segment = ind.trailing_valid_segment(bars)
    assert [b.close for b in segment] == [12.0, 13.0]


def test_trailing_valid_segment_is_empty_when_the_last_bar_is_invalid():
    bars = [
        _bar(0, 10.0, is_segment_start=True),
        _bar(1, 11.0, is_valid=False),
    ]
    assert ind.trailing_valid_segment(bars) == ()


def test_trailing_valid_segment_excludes_bars_before_an_invalid_gap():
    bars = [
        _bar(0, 10.0, is_segment_start=True),
        _bar(1, 11.0, is_valid=False),
        _bar(2, 12.0, is_segment_start=True),
        _bar(3, 13.0),
    ]
    segment = ind.trailing_valid_segment(bars)
    assert [b.close for b in segment] == [12.0, 13.0]
