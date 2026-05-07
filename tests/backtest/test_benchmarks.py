"""Tests for backtest benchmark strategies."""

import sys
from pathlib import Path

import pandas as pd
import pytest

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare
from benchmarks.buy_and_hold import BuyAndHold
from benchmarks.random_baseline import RandomBaseline


def _make_bar_data(symbol: str, date: str, close: float) -> prepare.BarData:
    """Helper to create BarData with required fields."""
    history = pd.DataFrame({"close": [close]})
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


class TestBuyAndHold:
    """Tests for buy-and-hold benchmark."""

    def test_exposes_on_bar(self):
        """Test that strategy exposes on_bar method."""
        strategy = BuyAndHold()
        assert hasattr(strategy, "on_bar")
        assert callable(strategy.on_bar)

    def test_buys_once_on_first_day(self):
        """Test that strategy buys only on first day."""
        strategy = BuyAndHold()

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-01", 100.0),
            "ETH": _make_bar_data("ETH", "2025-04-01", 50.0),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # First day should generate buy signals
        signals = strategy.on_bar(bar_data, portfolio)

        assert len(signals) == 2
        assert all(s.action == "buy" for s in signals)
        # Equal weight: 1.0 / 2 = 0.5
        assert all(s.weight == pytest.approx(0.5) for s in signals)

    def test_no_buys_after_first_day(self):
        """Test that strategy doesn't buy after first day."""
        strategy = BuyAndHold()

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-02", 105.0),
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 0.5},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
        )

        # Second day should not generate signals (already bought)
        signals = strategy.on_bar(bar_data, portfolio)

        assert len(signals) == 0


class TestRandomBaseline:
    """Tests for random baseline benchmark."""

    def test_exposes_on_bar(self):
        """Test that strategy exposes on_bar method."""
        strategy = RandomBaseline()
        assert hasattr(strategy, "on_bar")
        assert callable(strategy.on_bar)

    def test_deterministic_under_same_seed(self):
        """Test that strategy is deterministic with same seed."""
        strategy1 = RandomBaseline(seed=42)
        strategy2 = RandomBaseline(seed=42)

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-01", 100.0),
            "ETH": _make_bar_data("ETH", "2025-04-01", 50.0),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        signals1 = strategy1.on_bar(bar_data, portfolio)
        signals2 = strategy2.on_bar(bar_data, portfolio)

        # Both should generate same signals (or both empty)
        assert len(signals1) == len(signals2)
        if signals1:
            assert signals1[0].symbol == signals2[0].symbol
            assert signals1[0].action == signals2[0].action

    def test_no_sell_for_unheld_symbols(self):
        """Test that strategy never generates sell for unheld symbols."""
        strategy = RandomBaseline(seed=42, action_probability=1.0)

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-01", 100.0),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},  # No positions held
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Generate many signals to check for invalid sells
        for _ in range(100):
            signals = strategy.on_bar(bar_data, portfolio)
            for signal in signals:
                if signal.action == "sell":
                    assert signal.symbol in portfolio.positions, (
                        f"Sell signal for unheld symbol: {signal.symbol}"
                    )

    def test_only_buys_or_sells_held(self):
        """Test that strategy only buys or sells held positions."""
        strategy = RandomBaseline(seed=123, action_probability=1.0)

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-01", 100.0),
            "ETH": _make_bar_data("ETH", "2025-04-01", 50.0),
        }

        # Start with BTC held
        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 90.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
        )

        # Generate signals multiple times
        for _ in range(50):
            signals = strategy.on_bar(bar_data, portfolio)
            for signal in signals:
                if signal.action == "buy":
                    # Can only buy if not already held
                    assert (
                        signal.symbol not in portfolio.positions
                        or portfolio.positions[signal.symbol] == 0
                    )
                elif signal.action == "sell":
                    # Can only sell if held
                    assert signal.symbol in portfolio.positions

    def test_no_shorting(self):
        """Test that strategy never generates short positions."""
        strategy = RandomBaseline(seed=42, action_probability=1.0)

        bar_data = {
            "BTC": _make_bar_data("BTC", "2025-04-01", 100.0),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Generate many signals to verify no shorting
        for _ in range(100):
            signals = strategy.on_bar(bar_data, portfolio)
            for signal in signals:
                # Never short - only positive quantities
                if signal.action == "buy":
                    assert signal.weight > 0
