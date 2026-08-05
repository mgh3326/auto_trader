from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from research.crypto_stage_b.signals import evaluate_signal, nearest_rank_quantile
from research.crypto_stage_b.source import DailyBar
from research.crypto_stage_b.tests.conftest import candidate, etr_bars


def test_nearest_rank_uses_ceil_rank_not_interpolation_or_floor() -> None:
    assert nearest_rank_quantile((1.0, 2.0, 3.0, 4.0), 0.25) == 1.0
    assert nearest_rank_quantile((1.0, 2.0, 3.0, 4.0), 0.30) == 2.0


def test_etr_quantile_excludes_current_bar() -> None:
    start = date(2024, 1, 1)
    closes = [100.0]
    # Q_0.10 over this 250-return historical sample is -0.10.
    for value in [-0.10] * 25 + [0.0] * 225:
        closes.append(closes[-1] * math.exp(value))
    closes.append(closes[-1] * math.exp(-0.05))
    bars = []
    for index, close in enumerate(closes):
        is_current = index == len(closes) - 1
        bars.append(
            DailyBar(
                venue="upbit_krw",
                symbol="KRW-Q",
                session=start + timedelta(days=index),
                open=close,
                high=close * 1.10 if is_current else close * 1.01,
                low=close * 0.50 if is_current else close * 0.99,
                close=close,
                base_volume=100.0,
                quote_volume=200.0 if is_current else 100.0,
            )
        )

    result = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars, arm="full")

    assert result.eligible is True
    assert result.metrics["q10_r1"] == pytest.approx(-0.10)
    assert result.metrics["r1"] == pytest.approx(-0.05)
    assert result.signal is False


def test_etr_ablation_is_not_a_full_signal_fallback() -> None:
    bars = etr_bars(quote_volume_on_signal=100.0)
    full = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars[:252], arm="full")
    ablation = evaluate_signal(candidate("CR-SPOT-ETR-01"), bars[:252], arm="ablation")

    assert full.signal is False
    assert ablation.signal is True


def test_tpr_uses_six_inclusive_integrity_observations_t_minus_5_through_t() -> None:
    start = date(2024, 1, 1)
    bars: list[DailyBar] = []
    for index in range(120):
        close = 100.0 + 0.1 * index if index <= 99 else 110.0
        if 114 <= index <= 118:
            close = 107.0
        if index == 119:
            close = 110.0
        open_price = close - 1.0 if index == 119 else close
        bars.append(
            DailyBar(
                venue="upbit_krw",
                symbol="KRW-T",
                session=start + timedelta(days=index),
                open=open_price,
                high=111.0,
                low=min(open_price, close) - 1.0,
                close=close,
                base_volume=100.0,
                quote_volume=100.0,
            )
        )

    result = evaluate_signal(candidate("CR-SPOT-TPR-01"), bars, arm="full")

    assert result.signal is True
    assert result.stages["trend_integrity"] is True


def test_ceb_uses_100_observation_compression_reference_before_current_bar() -> None:
    start = date(2024, 1, 1)
    bars: list[DailyBar] = []
    for index in range(122):
        open_price, high, low, close, quote_volume = (100.0, 101.0, 99.0, 100.0, 100.0)
        if index == 121:
            open_price, high, low, close, quote_volume = (
                100.0,
                106.0,
                95.0,
                105.0,
                300.0,
            )
        bars.append(
            DailyBar(
                venue="binance_usdt_spot",
                symbol="CEBUSDT",
                session=start + timedelta(days=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                base_volume=100.0,
                quote_volume=quote_volume,
            )
        )

    result = evaluate_signal(candidate("CR-SPOT-CEB-01"), bars, arm="full")

    assert result.signal is True
    assert result.stages["compression_state"] is True
    assert result.stages["raw_20d_breakout"] is True
