"""Signal, volatility and sizing rules on synthetic bars (no network)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from research.crypto_trend.signal import (
    BAR_MS,
    DAY_MS,
    EWMA_DAYS,
    EWMA_LAMBDA,
    SIGMA_STAR,
    entry_notional,
    ewma_annual_vol,
    last_completed_day_open,
    momentum_sign,
    vol_scale,
)

T0 = int(datetime(2024, 1, 10, tzinfo=UTC).timestamp() * 1000)  # midnight UTC


@pytest.mark.unit
def test_momentum_sign_follows_the_return_over_the_lookback() -> None:
    # decision closes at T0; the lookback bar opens one day earlier.
    now_open = T0 - BAR_MS
    past_open = now_open - DAY_MS
    up = {now_open: 110.0, past_open: 100.0}
    down = {now_open: 90.0, past_open: 100.0}
    flat = {now_open: 100.0, past_open: 100.0}
    assert momentum_sign(up, T0, 1) == 1
    assert momentum_sign(down, T0, 1) == -1
    assert momentum_sign(flat, T0, 1) == 0


@pytest.mark.unit
def test_momentum_sign_is_no_signal_when_either_bar_is_missing() -> None:
    now_open = T0 - BAR_MS
    past_open = now_open - 60 * DAY_MS
    assert momentum_sign({now_open: 110.0}, T0, 60) is None  # lookback missing
    assert momentum_sign({past_open: 100.0}, T0, 60) is None  # decision bar missing


@pytest.mark.unit
def test_last_completed_day_is_the_newest_daily_close_known_at_t() -> None:
    # A daily close is known at its day's end (00:00 UTC of the next day).
    assert last_completed_day_open(T0) == T0 - DAY_MS  # 00:00 closes the prior day
    assert last_completed_day_open(T0 + 4 * 3_600_000) == T0 - DAY_MS  # 04:00
    assert last_completed_day_open(T0 - BAR_MS) == T0 - 2 * DAY_MS  # 20:00 prior


@pytest.mark.unit
def test_ewma_vol_matches_the_closed_form_for_one_nonzero_return() -> None:
    # Only the most recent return is non-zero. With weights lambda**i over the
    # EWMA_DAYS returns, variance = r^2 (1 - lambda) / (1 - lambda**EWMA_DAYS).
    last = T0 - DAY_MS
    days = [last - i * DAY_MS for i in range(EWMA_DAYS + 1)]
    closes = dict.fromkeys(days, 100.0)
    closes[last] = 102.0
    r = 0.02
    variance = r * r * (1 - EWMA_LAMBDA) / (1 - EWMA_LAMBDA**EWMA_DAYS)
    expected = math.sqrt(variance * 365)
    assert ewma_annual_vol(closes, last) == pytest.approx(expected, rel=1e-12)


@pytest.mark.unit
def test_ewma_vol_is_no_signal_on_a_gap_or_zero_variance() -> None:
    last = T0 - DAY_MS
    days = [last - i * DAY_MS for i in range(EWMA_DAYS + 1)]
    flat = dict.fromkeys(days, 100.0)
    assert ewma_annual_vol(flat, last) is None  # zero variance is undefined
    gapped = {d: 100.0 + i for i, d in enumerate(days) if d != days[7]}
    assert ewma_annual_vol(gapped, last) is None  # a missing day inside the window


@pytest.mark.unit
def test_vol_scale_never_exceeds_one_and_shrinks_with_volatility() -> None:
    assert vol_scale(SIGMA_STAR / 2) == 1.0  # calm: capped, never scaled up
    assert vol_scale(SIGMA_STAR * 2) == pytest.approx(0.5)
    assert entry_notional(60.0, SIGMA_STAR * 2) == pytest.approx(30.0)
    assert entry_notional(60.0, SIGMA_STAR / 3) == pytest.approx(60.0)
