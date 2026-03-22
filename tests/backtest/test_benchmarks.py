"""Tests for backtest benchmark strategies."""

import sys
from pathlib import Path

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare
from benchmarks.buy_and_hold import BuyAndHold
from benchmarks.random_baseline import RandomBaseline


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
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
            ),
            "ETH": prepare.BarData(
                date="2025-04-01",
                open=50.0,
                high=55.0,
                low=45.0,
                close=50.0,
                volume=2000,
                value=200000,
            ),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # First day should generate buy signals
        signals = strategy.on_bar("2025-04-01", bar_data, portfolio, 0)

        assert len(signals) == 2
        assert all(s.action == "buy" for s in signals)
        # Equal weight: 1.0 / 2 = 0.5
        assert all(s.target_weight == 0.5 for s in signals)

    def test_no_buys_after_first_day(self):
        """Test that strategy doesn't buy after first day."""
        strategy = BuyAndHold()

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-02",
                open=105.0,
                high=115.0,
                low=95.0,
                close=105.0,
                volume=1000,
                value=100000,
            ),
        }

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 0.5},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
        )

        # Second day should not generate signals
        signals = strategy.on_bar("2025-04-02", bar_data, portfolio, 1)

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
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
            ),
            "ETH": prepare.BarData(
                date="2025-04-01",
                open=50.0,
                high=55.0,
                low=45.0,
                close=50.0,
                volume=2000,
                value=200000,
            ),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        signals1 = strategy1.on_bar("2025-04-01", bar_data, portfolio, 0)
        signals2 = strategy2.on_bar("2025-04-01", bar_data, portfolio, 0)

        # Both should generate same signals (or both empty)
        assert len(signals1) == len(signals2)
        if signals1:
            assert signals1[0].symbol == signals2[0].symbol
            assert signals1[0].action == signals2[0].action

    def test_no_sell_for_unheld_symbols(self):
        """Test that strategy never generates sell for unheld symbols."""
        strategy = RandomBaseline(seed=42, action_probability=1.0)

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
            ),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},  # No positions held
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Generate many signals to check for invalid sells
        for i in range(100):
            signals = strategy.on_bar("2025-04-01", bar_data, portfolio, i)
            for signal in signals:
                if signal.action == "sell":
                    assert signal.symbol in portfolio.positions, \
                        f"Sell signal for unheld symbol: {signal.symbol}"

    def test_only_buys_or_sells_held(self):
        """Test that strategy only buys or sells held positions."""
        strategy = RandomBaseline(seed=123, action_probability=1.0)

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
            ),
            "ETH": prepare.BarData(
                date="2025-04-01",
                open=50.0,
                high=55.0,
                low=45.0,
                close=50.0,
                volume=2000,
                value=200000,
            ),
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
        for i in range(50):
            signals = strategy.on_bar("2025-04-01", bar_data, portfolio, i)
            for signal in signals:
                if signal.action == "buy":
                    # Can only buy if not already held
                    assert signal.symbol not in portfolio.positions or \
                        portfolio.positions[signal.symbol] == 0
                elif signal.action == "sell":
                    # Can only sell if held
                    assert signal.symbol in portfolio.positions

    def test_no_shorting(self):
        """Test that strategy never generates short positions."""
        strategy = RandomBaseline(seed=42, action_probability=1.0)

        bar_data = {
            "BTC": prepare.BarData(
                date="2025-04-01",
                open=100.0,
                high=110.0,
                low=90.0,
                close=100.0,
                volume=1000,
                value=100000,
            ),
        }

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
        )

        # Generate many signals to verify no shorting
        for i in range(100):
            signals = strategy.on_bar("2025-04-01", bar_data, portfolio, i)
            for signal in signals:
                # Never short - only positive quantities
                if signal.action == "buy":
                    assert signal.target_weight > 0
