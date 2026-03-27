"""Tests for RSI indicator calculations."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.indicators import calc_rsi, calc_rsi_series


class TestCalcRsi:
    """Tests for single-point RSI calculation."""

    def test_insufficient_data_returns_none(self):
        closes = np.array([100.0, 101.0, 102.0])
        assert calc_rsi(closes, period=14) is None

    def test_all_gains_returns_100(self):
        closes = np.arange(100.0, 116.0, dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert rsi == pytest.approx(100.0, abs=0.01)

    def test_all_losses_returns_near_zero(self):
        closes = np.arange(200.0, 184.0, -1.0, dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert rsi < 1.0

    def test_known_value(self):
        rng = np.random.default_rng(42)
        base = 100.0
        prices = [base]
        for _ in range(100):
            change = rng.choice([-1.0, 1.0])
            prices.append(prices[-1] + change)
        closes = np.array(prices)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert 30.0 < rsi < 70.0

    def test_period_6(self):
        closes = np.arange(100.0, 108.0, dtype=float)
        rsi = calc_rsi(closes, period=6)
        assert rsi is not None
        assert rsi == pytest.approx(100.0, abs=0.01)


class TestCalcRsiSeries:
    """Tests for full-series RSI calculation."""

    def test_output_length_matches_input(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert len(result) == len(closes)

    def test_first_period_values_are_nan(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert all(np.isnan(result[:14]))

    def test_values_after_period_are_not_nan(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert not np.isnan(result[14])

    def test_last_value_matches_calc_rsi(self):
        rng = np.random.default_rng(99)
        closes = 100.0 + np.cumsum(rng.normal(0, 1, 50))
        series_val = calc_rsi_series(closes, period=14)[-1]
        point_val = calc_rsi(closes, period=14)
        assert series_val == pytest.approx(point_val, abs=0.01)

    def test_insufficient_data_all_nan(self):
        closes = np.array([100.0, 101.0, 102.0])
        result = calc_rsi_series(closes, period=14)
        assert all(np.isnan(result))
