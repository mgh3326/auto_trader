"""Tests for CryptoVotingSignals evaluator."""

import numpy as np
import pandas as pd
import pytest

from app.services.crypto_voting_signals import CryptoVotingSignals


@pytest.fixture
def evaluator():
    return CryptoVotingSignals()


def _make_ohlcv_df(closes: list[float], volumes: list[float] | None = None):
    """Create a minimal OHLCV DataFrame for testing."""
    n = len(closes)
    if volumes is None:
        volumes = [1000.0] * n
    return pd.DataFrame(
        {
            "open": closes,  # simplified
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


class TestCryptoVotingSignals:
    def test_insufficient_history_returns_none(self, evaluator):
        df = _make_ohlcv_df([100.0] * 10)  # too few bars
        result = evaluator.evaluate(df)
        assert result is None

    def test_oversold_with_volume_spike_gives_high_bull_votes(self, evaluator):
        # Create data that produces RSI < 30, volume spike, etc.
        # Descending prices = oversold RSI
        closes = list(np.linspace(200, 100, 50))
        volumes = [1000.0] * 30 + [5000.0] * 20  # volume spike at end
        df = _make_ohlcv_df(closes, volumes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert result.bull_votes >= 1  # at minimum dual_rsi should fire
        assert isinstance(result.bull_flags, dict)
        assert isinstance(result.bear_flags, dict)

    def test_result_has_all_fields(self, evaluator):
        closes = list(np.linspace(100, 200, 50)) + list(np.linspace(200, 150, 10))
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert hasattr(result, "rsi_fast")
        assert hasattr(result, "rsi_slow")
        assert hasattr(result, "bull_votes")
        assert hasattr(result, "bear_votes")
        assert hasattr(result, "bull_flags")
        assert hasattr(result, "bear_flags")
        assert hasattr(result, "buy_signal")
        assert hasattr(result, "sell_signal")
        assert len(result.bull_flags) == 6
        assert len(result.bear_flags) == 5

    def test_buy_signal_requires_min_votes(self, evaluator):
        # MIN_VOTES = 4, so < 4 bull votes = no buy
        closes = list(np.linspace(100, 200, 50))  # uptrend = not oversold
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert result.buy_signal is False  # uptrend won't trigger enough bull signals

    def test_all_flags_are_native_bool(self, evaluator):
        """Regression #463: numpy.bool_ breaks FastMCP structured output."""
        # Uptrend then reversal — activates most indicators
        closes = list(np.linspace(100, 200, 40)) + list(np.linspace(200, 130, 10))
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        for key, value in result.bull_flags.items():
            assert type(value) is bool, f"bull_flags[{key}] is {type(value)}, not bool"
        for key, value in result.bear_flags.items():
            assert type(value) is bool, f"bear_flags[{key}] is {type(value)}, not bool"
        assert type(result.buy_signal) is bool, "buy_signal is not native bool"
        assert type(result.sell_signal) is bool, "sell_signal is not native bool"
