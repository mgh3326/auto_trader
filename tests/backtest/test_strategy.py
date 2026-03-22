"""Tests for backtest strategy module."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import prepare
import strategy


def _make_history(
    closes: list[float],
    volumes: list[float] | None = None,
    dates: list[str] | None = None,
) -> pd.DataFrame:
    """Create a history DataFrame with custom close/volume series."""
    n = len(closes)
    if dates is None:
        dates = (
            pd.date_range(end="2025-04-01", periods=n, freq="D")
            .strftime("%Y-%m-%d")
            .tolist()
        )
    if volumes is None:
        volumes = [1000.0] * n

    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": volumes,
            "value": [v * c for v, c in zip(volumes, closes)],
        },
        index=dates,
    )


def _make_bar_data(
    symbol: str,
    date: str,
    close: float,
    history: pd.DataFrame | None = None,
) -> prepare.BarData:
    """Helper to create BarData with configurable history."""
    if history is None:
        # Create default history with enough data for RSI calculation
        closes = [close] * 30
        history = _make_history(closes)

    return prepare.BarData(
        symbol=symbol,
        date=date,
        open=close * 0.99,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=1000,
        value=100000,
        history=history,
    )


def _make_oversold_history(periods: int = 30) -> pd.DataFrame:
    """Create a history with oversold RSI pattern (downtrend then flat low)."""
    # Start high, decline sharply to create oversold condition
    closes = list(range(150, 150 - periods, -1))[:periods]
    # Ensure we have enough data
    if len(closes) < periods:
        closes = [150] * (periods - len(closes)) + closes
    return _make_history(closes)


def _make_overbought_history(periods: int = 30) -> pd.DataFrame:
    """Create a history with overbought RSI pattern (strong uptrend)."""
    closes = list(range(100, 100 + periods))
    return _make_history(closes)


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


class TestIndicatorHelpers:
    """Tests for indicator helper functions."""

    def test_calc_ema_tracks_uptrend(self):
        """Test EMA calculation on an uptrend."""
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        ema = strategy._calc_ema(closes, span=3)
        assert ema is not None
        assert ema[-1] > ema[0]  # EMA should increase in uptrend

    def test_calc_ema_insufficient_history_returns_none(self):
        """Test EMA returns None with insufficient history."""
        closes = np.array([1.0, 2.0])  # Only 2 bars, need at least span=5
        ema = strategy._calc_ema(closes, span=5)
        assert ema is None

    def test_calc_macd_returns_values_with_enough_history(self):
        """Test MACD calculation with sufficient history."""
        # Create enough data points: slow (26) + signal (9) = 35 minimum
        closes = np.array([100.0 + i * 0.5 for i in range(40)])
        macd_result = strategy._calc_macd(closes, fast=12, slow=26, signal=9)
        assert macd_result is not None
        macd_line, signal_line, histogram = macd_result
        assert isinstance(macd_line, float)
        assert isinstance(signal_line, float)
        assert isinstance(histogram, float)

    def test_calc_macd_insufficient_history_returns_none(self):
        """Test MACD returns None with insufficient history."""
        closes = np.array([100.0] * 10)  # Not enough for slow + signal
        macd_result = strategy._calc_macd(closes, fast=12, slow=26, signal=9)
        assert macd_result is None

    def test_calc_bollinger_returns_bands_with_enough_history(self):
        """Test Bollinger Bands calculation."""
        closes = np.array([100.0] * 20 + [110.0])  # 21 points, period=20
        bands = strategy._calc_bollinger(closes, period=20, std_mult=2.0)
        assert bands is not None
        upper, middle, lower = bands
        assert upper > middle > lower

    def test_calc_bollinger_insufficient_history_returns_none(self):
        """Test Bollinger returns None with insufficient history."""
        closes = np.array([100.0] * 10)
        bands = strategy._calc_bollinger(closes, period=20, std_mult=2.0)
        assert bands is None

    def test_calc_momentum_positive_in_uptrend(self):
        """Test momentum is positive in uptrend."""
        closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        momentum = strategy._calc_momentum(closes, period=5)
        assert momentum is not None
        assert momentum > 0  # Price increased from 100 to 105

    def test_calc_momentum_negative_in_downtrend(self):
        """Test momentum is negative in downtrend."""
        closes = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
        momentum = strategy._calc_momentum(closes, period=5)
        assert momentum is not None
        assert momentum < 0  # Price decreased from 100 to 95

    def test_calc_momentum_insufficient_history_returns_none(self):
        """Test momentum returns None with insufficient history."""
        closes = np.array([100.0, 101.0])
        momentum = strategy._calc_momentum(closes, period=5)
        assert momentum is None

    def test_calc_average_volume_with_enough_data(self):
        """Test average volume calculation."""
        volumes = np.array([1000.0] * 20 + [2000.0])
        avg_vol = strategy._calc_average_volume(volumes, lookback=20)
        assert avg_vol is not None
        assert avg_vol == 1050.0  # Average of 20 1000s and 1 2000

    def test_calc_average_volume_insufficient_history_returns_none(self):
        """Test average volume returns None with insufficient history."""
        volumes = np.array([1000.0] * 10)
        avg_vol = strategy._calc_average_volume(volumes, lookback=20)
        assert avg_vol is None


class TestBuySignals:
    """Tests for buy signal generation."""

    def test_buy_when_dual_rsi_oversold(self):
        """Test buy signal when both RSI fast and slow are oversold."""
        strat = strategy.Strategy()

        # Create oversold history (strong downtrend)
        history = _make_oversold_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 120.0, history)}

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

        # Should generate buy signal due to oversold RSI
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 1
        assert buy_signals[0].symbol == "BTC"

    def test_no_buy_when_already_held(self):
        """Test no buy signal when symbol is already held."""
        strat = strategy.Strategy()

        history = _make_oversold_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 120.0, history)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={"BTC": 1.0},  # Already holding BTC
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=100000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # No buy signals for already-held symbol
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 0

    def test_no_buy_when_max_positions_reached(self):
        """Test no buy signal when max positions is reached."""
        strat = strategy.Strategy()

        history = _make_oversold_history(30)
        bar_data = {"SOL": _make_bar_data("SOL", "2025-04-01", 120.0, history)}

        portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={
                "BTC": 1.0,
                "ETH": 1.0,
                "XRP": 1.0,
                "LINK": 1.0,
                "ADA": 1.0,
            },  # At max (5 positions)
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

        signals = strat.on_bar(bar_data, portfolio)

        # No buy signals when at max positions
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 0

    def test_buy_has_configured_weight(self):
        """Test that buy signal has the configured position size."""
        strat = strategy.Strategy()

        history = _make_oversold_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 120.0, history)}

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

        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 1
        assert buy_signals[0].weight == strategy.POSITION_SIZE


class TestSellSignals:
    """Tests for sell signal generation."""

    def test_sell_on_rsi_recovery_when_profitable(self):
        """Test sell when RSI recovers above exit threshold and position is profitable."""
        strat = strategy.Strategy()

        # Create overbought history (strong uptrend - RSI should be high)
        history = _make_overbought_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 128.0, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},  # Low entry, should be profitable
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Should sell due to RSI recovery when profitable
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert "RSI" in sell_signals[0].reason or "recovered" in sell_signals[0].reason.lower()

    def test_sell_on_stop_loss(self):
        """Test sell signal on stop-loss trigger."""
        strat = strategy.Strategy()

        # Create flat history
        history = _make_history([100.0] * 30)
        # Current price is below stop-loss threshold
        current_price = 85.0  # 15% below avg price of 100
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", current_price, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=135000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Should sell due to stop-loss
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) >= 1
        assert any("stop" in s.reason.lower() or "loss" in s.reason.lower() for s in sell_signals)

    def test_sell_when_holding_period_exceeded(self):
        """Test sell when holding period exceeded."""
        strat = strategy.Strategy()

        # Create history that keeps RSI below exit threshold (46)
        # Using a pattern that doesn't trigger RSI recovery
        closes = list(range(100, 70, -1)) + [70] * 20  # Downtrend then flat low
        history = _make_history(closes)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-22", 75.0, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 60.0},  # Avg buy price (profitable at 75)
            position_dates={"BTC": "2025-04-01"},  # 21 days ago
            trade_log=[],
            equity=155000.0,
            date="2025-04-22",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Should sell due to max holding period (RSI won't recover with downtrend)
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) >= 1
        assert any("holding" in s.reason.lower() or "max" in s.reason.lower() for s in sell_signals)

    def test_no_sell_when_holding_period_exceeded_but_unprofitable(self):
        """Test no RSI recovery sell when position is unprofitable."""
        strat = strategy.Strategy()

        # Create overbought history (RSI should be high)
        history = _make_overbought_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 128.0, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 150.0},  # Avg buy price (higher than current - unprofitable)
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=135000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Should NOT sell due to RSI recovery (not profitable)
        # But might sell due to stop-loss if price is low enough
        rsi_sells = [s for s in signals if s.action == "sell" and "RSI" in s.reason]
        assert len(rsi_sells) == 0


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
        assert len(signals) >= 1
        assert any(s.action == "sell" for s in signals)

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
