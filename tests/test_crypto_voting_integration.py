"""Integration tests: voting signals consistency between backtest and live."""

import numpy as np
import pandas as pd

from app.services.crypto_voting_signals import CryptoVotingSignals


class TestVotingBacktestConsistency:
    """Verify live voting evaluator matches backtest behavior."""

    def test_same_parameters_as_backtest(self):
        from app.services.crypto_voting_signals import (
            BB_PERIOD,
            BB_STD,
            EMA_FAST,
            EMA_SLOW,
            MACD_FAST,
            MACD_SIGNAL,
            MACD_SLOW,
            MIN_SELL_VOTES,
            MIN_VOTES,
            MOMENTUM_PERIOD,
            RSI_EXIT,
            RSI_OVERSOLD,
            RSI_PERIOD_FAST,
            RSI_PERIOD_SLOW,
            VOLUME_LOOKBACK,
            VOLUME_THRESHOLD,
        )

        # These must match backtest/strategy.py
        assert RSI_PERIOD_FAST == 7
        assert RSI_PERIOD_SLOW == 14
        assert RSI_OVERSOLD == 30
        assert RSI_EXIT == 46
        assert MIN_VOTES == 4
        assert MIN_SELL_VOTES == 2
        assert MACD_FAST == 12
        assert MACD_SLOW == 26
        assert MACD_SIGNAL == 9
        assert BB_PERIOD == 15
        assert BB_STD == 2.0
        assert EMA_FAST == 8
        assert EMA_SLOW == 24
        assert MOMENTUM_PERIOD == 5
        assert VOLUME_LOOKBACK == 20
        assert VOLUME_THRESHOLD == 1.5

    def test_bull_signal_count_is_six(self):
        evaluator = CryptoVotingSignals()
        # Create minimal valid data
        closes = list(np.linspace(200, 100, 50))
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes,
                "volume": [1000.0] * 50,
            }
        )
        result = evaluator.evaluate(df)
        assert result is not None
        assert len(result.bull_flags) == 6

    def test_bear_signal_count_is_five(self):
        evaluator = CryptoVotingSignals()
        closes = list(np.linspace(100, 200, 50))
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes,
                "volume": [1000.0] * 50,
            }
        )
        result = evaluator.evaluate(df)
        assert result is not None
        assert len(result.bear_flags) == 5

    def test_buy_signal_threshold_is_four_votes(self):
        """MIN_VOTES=4 requires at least 4 bull signals for buy_signal=True."""
        from app.services.crypto_voting_signals import MIN_VOTES

        assert MIN_VOTES == 4
        evaluator = CryptoVotingSignals()
        # Test that buy_signal is False when bull_votes < 4
        closes = list(np.linspace(100, 200, 50))
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes,
                "volume": [1000.0] * 50,
            }
        )
        result = evaluator.evaluate(df)
        assert result is not None
        assert result.bull_votes < MIN_VOTES
        assert result.buy_signal is False

    def test_sell_signal_threshold_is_two_votes(self):
        """MIN_SELL_VOTES=2 requires at least 2 bear signals for sell_signal=True."""
        from app.services.crypto_voting_signals import MIN_SELL_VOTES

        assert MIN_SELL_VOTES == 2
        evaluator = CryptoVotingSignals()
        # Test that sell_signal behavior works
        closes = list(np.linspace(100, 200, 50))
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes,
                "volume": [1000.0] * 50,
            }
        )
        result = evaluator.evaluate(df)
        assert result is not None
        # In an uptrend, we should have some bear signals but not enough for sell
        # This is just a smoke test that the logic exists
        assert isinstance(result.sell_signal, bool)

    def test_voting_result_has_all_expected_fields(self):
        """VotingResult should have all the fields expected by consumers."""
        evaluator = CryptoVotingSignals()
        closes = list(np.linspace(100, 200, 50))
        df = pd.DataFrame(
            {
                "open": closes,
                "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes],
                "close": closes,
                "volume": [1000.0] * 50,
            }
        )
        result = evaluator.evaluate(df)
        assert result is not None

        # Check all expected fields exist
        assert hasattr(result, "rsi_fast")
        assert hasattr(result, "rsi_slow")
        assert hasattr(result, "bull_votes")
        assert hasattr(result, "bear_votes")
        assert hasattr(result, "bull_flags")
        assert hasattr(result, "bear_flags")
        assert hasattr(result, "buy_signal")
        assert hasattr(result, "sell_signal")

        # Check to_dict works
        d = result.to_dict()
        assert "rsi_fast" in d
        assert "rsi_slow" in d
        assert "bull_votes" in d
        assert "bear_votes" in d
        assert "bull_flags" in d
        assert "bear_flags" in d
        assert "buy_signal" in d
        assert "sell_signal" in d
