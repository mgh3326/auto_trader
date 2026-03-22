"""Tests for backtest strategy module."""

import sys
from pathlib import Path
from unittest import mock

import numpy as np

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare
import strategy


class TestRSICalculation:
    """Tests for RSI calculation."""

    def test_rsi_returns_finite_value_with_enough_history(self):
        """Test that RSI returns a finite value when there's enough history."""
        closes = np.array([100.0, 102.0, 101.0, 103.0, 102.0, 104.0, 103.0, 105.0, 104.0, 106.0,
                          105.0, 107.0, 106.0, 108.0, 107.0, 109.0, 108.0, 110.0, 109.0, 111.0])

        rsi = strategy._calc_rsi(closes, period=14)

        assert np.isfinite(rsi)
        assert 0 <= rsi <= 100

    def test_rsi_insufficient_history_returns_none(self):
        """Test that RSI returns None with insufficient history."""
        closes = np.array([100.0, 102.0, 101.0, 103.0])  # Only 4 bars

        rsi = strategy._calc_rsi(closes, period=14)

        assert rsi is None


class TestBuySignals:
    """Tests for buy signal generation."""

    def test_buy_when_rsi_oversold(self):
        """Test buy signal when RSI is below oversold threshold."""
        strat = strategy.Strategy()

        # Create bar data with prices that indicate oversold
        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Pre-populate history with enough bars
        strat._history["BTC"] = [100.0] * 15

        # Mock RSI to be oversold
        with mock.patch.object(strat, "_get_rsi", return_value=25.0):
            signals = strat.on_bar("2025-04-01", bar_data, portfolio, 20)

        assert len(signals) == 1
        assert signals[0].symbol == "BTC"
        assert signals[0].action == "buy"

    def test_no_buy_when_already_held(self):
        """Test no buy signal when symbol is already held."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={"BTC": 1.0},  # Already holding BTC
            avg_prices={"BTC": 100.0},  # Higher than close to prevent holding period exit
            position_dates={"BTC": "2025-04-01"},  # Same as bar date
            trade_log=[],
        )

        # Pre-populate history
        strat._history["BTC"] = [100.0] * 15

        with mock.patch.object(strat, "_get_rsi", return_value=25.0):
            signals = strat.on_bar("2025-04-01", bar_data, portfolio, 20)

        assert len(signals) == 0

    def test_no_buy_when_max_positions_reached(self):
        """Test no buy signal when max positions is reached."""
        strat = strategy.Strategy()

        bar_data = {
            "SOL": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={"BTC": 1.0, "ETH": 1.0, "XRP": 1.0, "DOGE": 1.0},  # Already at max
            avg_prices={"BTC": 90.0, "ETH": 80.0, "XRP": 0.5, "DOGE": 0.1},
            position_dates={"BTC": "2025-03-25", "ETH": "2025-03-25", "XRP": "2025-03-25", "DOGE": "2025-03-25"},
            trade_log=[],
        )

        with mock.patch.object(strat, "_get_rsi", return_value=25.0):
            signals = strat.on_bar("2025-04-01", bar_data, portfolio, 20)

        assert len(signals) == 0

    def test_buy_has_configured_weight(self):
        """Test that buy signal has the configured position size."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Pre-populate history
        strat._history["BTC"] = [100.0] * 15

        with mock.patch.object(strat, "_get_rsi", return_value=25.0):
            signals = strat.on_bar("2025-04-01", bar_data, portfolio, 20)

        assert len(signals) == 1
        assert signals[0].target_weight == strategy.POSITION_SIZE


class TestSellSignals:
    """Tests for sell signal generation."""

    def test_full_sell_when_rsi_overbought(self):
        """Test full sell when RSI is above overbought threshold."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=105.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )

        # Pre-populate history
        strat._history["BTC"] = [100.0] * 15

        with mock.patch.object(strat, "_get_rsi", return_value=75.0):
            signals = strat.on_bar("2025-04-01", bar_data, portfolio, 20)

        assert len(signals) == 1
        assert signals[0].action == "sell"
        assert signals[0].target_weight == 0.0  # Full sell

    def test_sell_when_holding_period_exceeded_and_profitable(self):
        """Test sell when holding period exceeded and position is profitable."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-08",  # 7 days after entry
                open=100.0,
                high=110.0,
                low=90.0,
                close=105.0,  # Higher than avg price (profitable)
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},  # Avg buy price
            position_dates={"BTC": "2025-04-01"},  # 7 days ago
            trade_log=[],
        )

        # Pre-populate history
        strat._history["BTC"] = [100.0] * 15

        with mock.patch.object(strat, "_get_rsi", return_value=50.0):  # Not overbought
            signals = strat.on_bar("2025-04-08", bar_data, portfolio, 20)

        assert len(signals) == 1
        assert signals[0].action == "sell"

    def test_no_sell_when_holding_period_exceeded_but_unprofitable(self):
        """Test no sell when holding period exceeded but position is unprofitable."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-08",  # 7 days after entry
                open=100.0,
                high=110.0,
                low=90.0,
                close=85.0,  # Lower than avg price (unprofitable)
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},  # Avg buy price (higher than current)
            position_dates={"BTC": "2025-04-01"},  # 7 days ago
            trade_log=[],
        )

        # Pre-populate history
        strat._history["BTC"] = [100.0] * 15

        with mock.patch.object(strat, "_get_rsi", return_value=50.0):  # Not overbought
            signals = strat.on_bar("2025-04-08", bar_data, portfolio, 20)

        assert len(signals) == 0


class TestStrategyHistoryTracking:
    """Tests for strategy history tracking."""

    def test_strategy_tracks_close_prices(self):
        """Test that strategy tracks close prices for RSI calculation."""
        strat = strategy.Strategy()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        strat.on_bar("2025-04-01", bar_data, portfolio, 0)

        assert "BTC" in strat._history
        assert len(strat._history["BTC"]) == 1
        assert strat._history["BTC"][0] == 95.0

    def test_strategy_history_limited_to_lookback(self):
        """Test that history is limited to lookback bars."""
        strat = strategy.Strategy()
        strat._history["BTC"] = [100.0] * 25  # Exceeds LOOKBACK_BARS

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=95.0,
                volume=1000,
                value=100000,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        strat.on_bar("2025-04-01", bar_data, portfolio, 0)

        # Max history is RSI_PERIOD + LOOKBACK_BARS
        assert len(strat._history["BTC"]) <= strategy.RSI_PERIOD + strategy.LOOKBACK_BARS
