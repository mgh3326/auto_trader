"""
Tests for MCP indicator math calculation functions.

This module tests the _calculate_* functions from market_data_indicators module.
"""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_indicators

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sample_ohlcv_df(n: int = 250, include_date: bool = True) -> pd.DataFrame:
    """Create sample OHLCV DataFrame for indicator testing."""
    np.random.seed(42)
    base_price = 100.0
    prices = base_price + np.cumsum(np.random.randn(n) * 2)

    df = pd.DataFrame(
        {
            "open": prices + np.random.randn(n) * 0.5,
            "high": prices + abs(np.random.randn(n) * 1.5),
            "low": prices - abs(np.random.randn(n) * 1.5),
            "close": prices,
            "volume": np.random.randint(1000, 10000, n),
        }
    )

    if include_date:
        # Generate dates going back from today
        end_date = dt.date.today()
        dates = [end_date - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
        df["date"] = dates

    return df


def _fib_df_uptrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where low comes first, then high (uptrend)."""
    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 100 up to ~200
    close = np.linspace(100, 200, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 1,
            "high": close + 2,
            "low": close - 3,
            "close": close,
            "volume": [1000] * n,
        }
    )


def _fib_df_downtrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where high comes first, then low (downtrend)."""
    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 200 down to ~100
    close = np.linspace(200, 100, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close + 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": [1000] * n,
        }
    )


# ---------------------------------------------------------------------------
# SMA Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateSMA:
    """Tests for _calculate_sma function."""

    def test_calculates_sma_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = market_data_indicators._calculate_sma(df["close"])

        assert "5" in result
        assert "20" in result
        assert "60" in result
        assert "120" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_sma(df["close"])

        assert result["5"] is not None
        assert result["20"] is None
        assert result["200"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_sma(df["close"], periods=[5, 10, 25])

        assert "5" in result
        assert "10" in result
        assert "25" in result
        assert len(result) == 3


# ---------------------------------------------------------------------------
# EMA Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateEMA:
    """Tests for _calculate_ema function."""

    def test_calculates_ema_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = market_data_indicators._calculate_ema(df["close"])

        assert "5" in result
        assert "20" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_ema(df["close"])

        assert result["5"] is not None
        assert result["20"] is None

    def test_ema_differs_from_sma(self):
        df = _sample_ohlcv_df(50)
        sma = market_data_indicators._calculate_sma(df["close"], periods=[20])
        ema = market_data_indicators._calculate_ema(df["close"], periods=[20])

        # EMA gives more weight to recent prices, so values should differ
        assert sma["20"] != ema["20"]


# ---------------------------------------------------------------------------
# RSI Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateRSI:
    """Tests for _calculate_rsi function."""

    def test_calculates_rsi(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_rsi(df["close"])

        assert "14" in result
        assert result["14"] is not None
        # RSI should be between 0 and 100
        assert 0 <= result["14"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_rsi(df["close"])

        assert result["14"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_rsi(df["close"], period=7)

        assert "7" in result
        assert result["7"] is not None


# ---------------------------------------------------------------------------
# MACD Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateMACD:
    """Tests for _calculate_macd function."""

    def test_calculates_macd(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_macd(df["close"])

        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(20)
        result = market_data_indicators._calculate_macd(df["close"])

        assert result["macd"] is None
        assert result["signal"] is None
        assert result["histogram"] is None

    def test_histogram_equals_macd_minus_signal(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_macd(df["close"])

        assert result["macd"] is not None
        assert result["signal"] is not None
        assert result["histogram"] is not None
        expected_hist = result["macd"] - result["signal"]
        assert abs(result["histogram"] - expected_hist) < 0.01


# ---------------------------------------------------------------------------
# Bollinger Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateBollinger:
    """Tests for _calculate_bollinger function."""

    def test_calculates_bollinger_bands(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_bollinger(df["close"])

        assert "upper" in result
        assert "middle" in result
        assert "lower" in result
        assert all(v is not None for v in result.values())
        # Upper > middle > lower
        assert result["upper"] is not None
        assert result["middle"] is not None
        assert result["lower"] is not None
        assert result["upper"] > result["middle"] > result["lower"]

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_bollinger(df["close"])

        assert result["upper"] is None
        assert result["middle"] is None
        assert result["lower"] is None

    def test_middle_equals_sma(self):
        df = _sample_ohlcv_df(50)
        bollinger = market_data_indicators._calculate_bollinger(df["close"], period=20)
        sma = market_data_indicators._calculate_sma(df["close"], periods=[20])

        assert bollinger["middle"] is not None
        assert sma["20"] is not None
        assert abs(bollinger["middle"] - sma["20"]) < 0.01


# ---------------------------------------------------------------------------
# ATR Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateATR:
    """Tests for _calculate_atr function."""

    def test_calculates_atr(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_atr(
            df["high"], df["low"], df["close"]
        )

        assert "14" in result
        assert result["14"] is not None
        assert result["14"] > 0

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_atr(
            df["high"], df["low"], df["close"]
        )

        assert result["14"] is None


# ---------------------------------------------------------------------------
# Pivot Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculatePivot:
    """Tests for _calculate_pivot function."""

    def test_calculates_pivot_points(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        assert "p" in result
        assert "r1" in result
        assert "r2" in result
        assert "r3" in result
        assert "s1" in result
        assert "s2" in result
        assert "s3" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(1)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        assert result["p"] is None
        assert result["r1"] is None
        assert result["s1"] is None

    def test_pivot_ordering(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        # R3 > R2 > R1 > P > S1 > S2 > S3
        assert result["r3"] is not None
        assert result["r2"] is not None
        assert result["r1"] is not None
        assert result["s1"] is not None
        assert result["s2"] is not None
        assert result["s3"] is not None
        assert result["r3"] > result["r2"] > result["r1"]
        assert result["s1"] > result["s2"] > result["s3"]


# ---------------------------------------------------------------------------
# ADX Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateADX:
    """Tests for _calculate_adx function."""

    def test_calculates_adx(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"]
        )

        assert "adx" in result
        assert "plus_di" in result
        assert "minus_di" in result
        assert all(v is not None for v in result.values())
        assert result["adx"] is not None
        assert 0 <= result["adx"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"]
        )

        assert result["adx"] is None
        assert result["plus_di"] is None
        assert result["minus_di"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(60)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"], period=10
        )

        assert result["adx"] is not None
        assert result["plus_di"] is not None
        assert result["minus_di"] is not None


# ---------------------------------------------------------------------------
# StochRSI Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateStochRSI:
    """Tests for _calculate_stoch_rsi function."""

    def test_calculates_stoch_rsi(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(df["close"])

        assert "k" in result
        assert "d" in result
        assert result["k"] is not None
        assert result["d"] is not None
        assert 0 <= result["k"] <= 100
        assert 0 <= result["d"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_stoch_rsi(df["close"])

        assert result["k"] is None
        assert result["d"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=7, k_period=5, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None


# ---------------------------------------------------------------------------
# OBV Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateOBV:
    """Tests for _calculate_obv function."""

    def test_calculates_obv(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_obv(df["close"], df["volume"])

        assert "obv" in result
        assert "signal" in result
        assert "divergence" in result
        assert result["obv"] is not None
        assert result["signal"] is not None
        assert result["divergence"] in ("bullish", "bearish", "none")

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_obv(df["close"], df["volume"])

        assert result["obv"] is None
        assert result["signal"] is None
        assert result["divergence"] is None

    def test_bullish_divergence_detected(self):
        n = 30
        close = pd.Series([100.0] * n)
        volume = pd.Series([1000.0] * n)
        close.iloc[-5:] = pd.Series(
            [100.0, 98.0, 96.0, 98.0, 95.0],
            index=close.iloc[-5:].index,
            dtype=float,
        )
        volume.iloc[-5:] = pd.Series(
            [1000.0, 1000.0, 1000.0, 10000.0, 1000.0],
            index=volume.iloc[-5:].index,
            dtype=float,
        )

        result = market_data_indicators._calculate_obv(close, volume)

        assert result["divergence"] == "bullish"

    def test_bearish_divergence_detected(self):
        n = 30
        close = pd.Series([95.0] * n)
        volume = pd.Series([1000.0] * n)
        close.iloc[-5:] = pd.Series(
            [95.0, 97.0, 99.0, 97.0, 100.0],
            index=close.iloc[-5:].index,
            dtype=float,
        )
        volume.iloc[-5:] = pd.Series(
            [1000.0, 1000.0, 1000.0, 10000.0, 1000.0],
            index=volume.iloc[-5:].index,
            dtype=float,
        )

        result = market_data_indicators._calculate_obv(close, volume)

        assert result["divergence"] == "bearish"

    def test_signal_is_ema_not_sma(self):
        close = pd.Series([100.0 + i * 0.5 for i in range(30)])
        volume = pd.Series([1000.0] * 30)

        result = market_data_indicators._calculate_obv(close, volume, signal_period=10)

        assert result["signal"] is not None
        direction = np.where(
            close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0)
        )
        obv = (volume * direction).cumsum()
        expected_signal = obv.ewm(span=10, adjust=False).mean().iloc[-1]
        assert result["signal"] == pytest.approx(
            round(float(expected_signal), 2), abs=0.01
        )


# ---------------------------------------------------------------------------
# ADX Regression Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestADXRegression:
    """Regression tests for ADX DM calculation fix."""

    def test_dm_independent_filtering(self):
        high = pd.Series([100, 105, 100, 100, 102, 101])
        low = pd.Series([95, 94, 95, 95, 94, 94])
        close = pd.Series([98, 103, 98, 98, 100, 99])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["adx"] is not None
        assert result["plus_di"] is not None
        assert result["minus_di"] is not None

    def test_up_move_greater_than_down_move(self):
        high = pd.Series([100, 105, 100, 100, 102, 101])
        low = pd.Series([95, 94, 95, 95, 94, 94])
        close = pd.Series([98, 103, 98, 98, 100, 99])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["plus_di"] is not None
        assert result["minus_di"] is not None

    def test_down_move_greater_than_up_move(self):
        high = pd.Series([100, 100, 100, 100, 100, 100])
        low = pd.Series([95, 90, 95, 95, 92, 92])
        close = pd.Series([98, 93, 98, 98, 96, 96])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["plus_di"] is not None
        assert result["minus_di"] is not None


# ---------------------------------------------------------------------------
# StochRSI Regression Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStochRSIRegression:
    """Regression tests for Stoch RSI calculation fix."""

    def test_returns_values_at_minimum_length_boundary(self):
        boundary_len = 14 + 3 + 3
        df = _sample_ohlcv_df(boundary_len)

        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None

    def test_uses_rsi_period_for_rolling_min_max(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None
        assert 0 <= result["k"] <= 100
        assert 0 <= result["d"] <= 100

    def test_k_is_smoothed_stoch_rsi(self):
        df = _sample_ohlcv_df(100)

        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None


# ---------------------------------------------------------------------------
# OBV Regression Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOBVRegression:
    """Regression tests for OBV calculation fix."""

    def test_signal_uses_ema(self):
        close = pd.Series([100.0 + i for i in range(30)])
        volume = pd.Series([1000.0] * 30)

        result = market_data_indicators._calculate_obv(close, volume, signal_period=10)

        direction = np.where(
            close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0)
        )
        obv = (volume * direction).cumsum()
        expected_signal = obv.ewm(span=10, adjust=False).mean().iloc[-1]

        assert result["signal"] is not None
        assert abs(result["signal"] - expected_signal) < 0.01

    def test_divergence_uses_lookback_plus_one_index(self):
        close = pd.Series(
            [100.0, 110.0, 90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 95.0]
        )
        volume = pd.Series([1000.0] * len(close))

        result = market_data_indicators._calculate_obv(close, volume, signal_period=5)

        # With lookback=10 and index -lookback-1, price_change<0 while obv_change>0 => bullish
        assert result["divergence"] == "bullish"


# ---------------------------------------------------------------------------
# Compute Indicators Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeIndicators:
    """Tests for _compute_indicators function."""

    def test_computes_single_indicator(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._compute_indicators(df, ["rsi"])

        assert "rsi" in result
        assert len(result) == 1

    def test_computes_multiple_indicators(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._compute_indicators(
            df, ["sma", "ema", "rsi", "macd"]
        )

        assert "sma" in result
        assert "ema" in result
        assert "rsi" in result
        assert "macd" in result

    def test_computes_all_indicators(self):
        df = _sample_ohlcv_df(250)
        all_indicators: list[market_data_indicators.IndicatorType] = [
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger",
            "atr",
            "pivot",
            "adx",
            "stoch_rsi",
            "obv",
        ]
        result = market_data_indicators._compute_indicators(df, all_indicators)

        for indicator in all_indicators:
            assert indicator in result

    def test_raises_on_missing_columns(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["atr"])

    def test_raises_on_missing_columns_for_adx(self):
        df = pd.DataFrame({"close": [1, 2, 3], "high": [4, 5, 6]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["adx"])

    def test_raises_on_missing_columns_for_obv(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["obv"])


# ---------------------------------------------------------------------------
# Fibonacci Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCalculateFibonacci:
    """Tests for _calculate_fibonacci helper."""

    def test_uptrend_retracement_from_high(self):
        df = _fib_df_uptrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "retracement_from_high"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing high, 100% level = swing low
        assert result["levels"]["0.0"] > result["levels"]["1.0"]

    def test_downtrend_bounce_from_low(self):
        df = _fib_df_downtrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "bounce_from_low"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing low, 100% level = swing high
        assert result["levels"]["0.0"] < result["levels"]["1.0"]

    def test_all_seven_levels_present(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        expected_keys = {"0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0"}
        assert set(result["levels"].keys()) == expected_keys

    def test_nearest_support_and_resistance(self):
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        swing_low = float(df["low"].min())
        mid = (swing_high + swing_low) / 2
        result = market_data_indicators._calculate_fibonacci(df, mid)

        if result["nearest_support"] is not None:
            assert result["nearest_support"]["price"] < mid
        if result["nearest_resistance"] is not None:
            assert result["nearest_resistance"]["price"] > mid

    def test_dates_are_strings(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        assert isinstance(result["swing_high"]["date"], str)
        assert isinstance(result["swing_low"]["date"], str)
        # ISO date format check
        assert len(result["swing_high"]["date"]) == 10
        assert len(result["swing_low"]["date"]) == 10

    def test_price_at_exact_level_no_crash(self):
        """If current price matches a level exactly, no crash."""
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        result = market_data_indicators._calculate_fibonacci(df, swing_high)

        assert result["current_price"] == swing_high
