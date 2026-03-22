"""Tests for backtest strategy module."""

import sys
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare
import strategy


def _make_bar_data(
    symbol: str, date: str, close: float, history_len: int = 20
) -> prepare.BarData:
    """Helper to create BarData with RSI-period history."""
    # Create history DataFrame with enough data for RSI calculation
    closes = [close] * (history_len + 1)
    dates = (
        pd.date_range(end=date, periods=history_len + 1, freq="D")
        .strftime("%Y-%m-%d")
        .tolist()
    )
    history = pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * (history_len + 1),
            "value": [100000.0] * (history_len + 1),
        }
    ).set_index("date")

    return prepare.BarData(
        symbol=symbol,
        date=date,
        open=close,
        high=close * 1.1,
        low=close * 0.9,
        close=close,
        volume=1000,
        value=100000,
        history=history,
    )


class TestRSICalculation:
    """Tests for RSI calculation."""

    def test_rsi_returns_finite_value_with_enough_history(self):
        """Test that RSI returns a finite value when there's enough history."""
        closes = np.array(
            [
                100.0,
                102.0,
                101.0,
                103.0,
                102.0,
                104.0,
                103.0,
                105.0,
                104.0,
                106.0,
                105.0,
                107.0,
                106.0,
                108.0,
                107.0,
                109.0,
                108.0,
                110.0,
                109.0,
                111.0,
            ]
        )

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
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 25.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 25.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 1
        assert signals[0].symbol == "BTC"
        assert signals[0].action == "buy"

    def test_no_buy_when_already_held(self):
        """Test no buy signal when symbol is already held."""
        strat = strategy.Strategy()

        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={"BTC": 1.0},  # Already holding BTC
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 25.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 25.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 0

    def test_no_buy_when_max_positions_reached(self):
        """Test no buy signal when max positions is reached."""
        strat = strategy.Strategy()

        bar_data = {"SOL": _make_bar_data("SOL", "2025-04-01", 95.0)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={
                "BTC": 1.0,
                "ETH": 1.0,
                "XRP": 1.0,
                "LINK": 1.0,
                "ADA": 1.0,
            },  # At max
            avg_prices={"BTC": 90.0, "ETH": 80.0, "XRP": 0.5, "LINK": 10.0, "ADA": 1.0},
            position_dates={
                "BTC": "2025-03-25",
                "ETH": "2025-03-25",
                "XRP": "2025-03-25",
                "LINK": "2025-03-25",
                "ADA": "2025-03-25",
            },
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 25.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 25.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 0

    def test_buy_has_configured_weight(self):
        """Test that buy signal has the configured position size."""
        strat = strategy.Strategy()

        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 95.0)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 25.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 25.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 1
        assert signals[0].weight == strategy.POSITION_SIZE


class TestSellSignals:
    """Tests for sell signal generation."""

    def test_full_sell_when_rsi_recovers_above_exit(self):
        """Test full sell when slow RSI recovers above the exit threshold."""
        strat = strategy.Strategy()

        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 105.0)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 55.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 50.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 1
        assert signals[0].action == "sell"
        assert signals[0].weight == 1.0  # Full sell

    def test_sell_when_holding_period_exceeded(self):
        """Test sell when the max holding period is exceeded."""
        strat = strategy.Strategy()

        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-24", 105.0)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},  # Avg buy price
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=155000.0,
            date="2025-04-24",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 45.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 40.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 1
        assert signals[0].action == "sell"

    def test_no_sell_before_recovery_or_holding_limit(self):
        """Test no sell when exit RSI and holding limits are not met."""
        strat = strategy.Strategy()

        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-08", 88.0)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},  # Avg buy price (higher than current)
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=135000.0,
            date="2025-04-08",
        )

        def mock_get_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 45.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 40.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=mock_get_rsi):
            signals = strat.on_bar(bar_data, portfolio)

        assert len(signals) == 0

    def test_stop_loss_records_cooldown_and_blocks_rebuy(self):
        """Test stop-loss exits and the cooldown prevents immediate re-entry."""
        strat = strategy.Strategy()

        stop_loss_bar = {"BTC": _make_bar_data("BTC", "2025-04-08", 84.0)}
        held_portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=134000.0,
            date="2025-04-08",
        )

        def neutral_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 45.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 40.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=neutral_rsi):
            exit_signals = strat.on_bar(stop_loss_bar, held_portfolio)

        assert len(exit_signals) == 1
        assert exit_signals[0].action == "sell"
        assert "Stop-loss" in exit_signals[0].reason

        rebuy_bar = {"BTC": _make_bar_data("BTC", "2025-04-12", 80.0)}
        empty_portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-12",
        )

        def oversold_rsi(bar: prepare.BarData, period: int) -> float | None:
            if period == strategy.RSI_PERIOD_FAST:
                return 20.0
            if period == strategy.RSI_PERIOD_SLOW:
                return 25.0
            return None

        with mock.patch.object(strat, "_get_rsi", side_effect=oversold_rsi):
            rebuy_signals = strat.on_bar(rebuy_bar, empty_portfolio)

        assert rebuy_signals == []


class TestStrategyWithHistory:
    """Tests for strategy using engine-provided history."""

    def test_strategy_gets_rsi_from_bar_history(self):
        """Test that strategy calculates RSI from BarData history."""
        strat = strategy.Strategy()

        # Create bar with rising price history (RSI > 50)
        dates = (
            pd.date_range("2025-03-01", "2025-04-01", freq="D")
            .strftime("%Y-%m-%d")
            .tolist()
        )
        n_bars = len(dates)
        # Create strong uptrend (RSI should be high/overbought)
        closes = list(range(100, 100 + n_bars))
        history = pd.DataFrame(
            {
                "open": closes,
                "high": [c + 1 for c in closes],
                "low": [c - 1 for c in closes],
                "close": closes,
                "volume": [1000.0] * n_bars,
                "value": [100000.0] * n_bars,
            },
            index=dates,
        )

        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=closes[-1],
                high=closes[-1] + 1,
                low=closes[-1] - 1,
                close=closes[-1],
                volume=1000,
                value=100000,
                history=history,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},  # Low entry, should be profitable
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        # With strong uptrend, RSI should be overbought (> 70)
        signals = strat.on_bar(bar_data, portfolio)

        # Should sell due to overbought RSI
        assert len(signals) == 1
        assert signals[0].action == "sell"

    def test_strategy_skips_insufficient_history(self):
        """Test that strategy skips symbols without enough history."""
        strat = strategy.Strategy()

        # Create bar with insufficient history (< RSI_PERIOD + 1)
        history = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.0, 101.0],
                "volume": [1000.0, 1000.0],
                "value": [100000.0, 100000.0],
            },
            index=["2025-03-31", "2025-04-01"],
        )

        bar_data = {
            "BTC": prepare.BarData(
                symbol="BTC",
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=101.0,
                volume=1000,
                value=100000,
                history=history,
            )
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # No signals due to insufficient history
        assert len(signals) == 0
