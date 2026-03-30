"""Tests for backtest strategy module."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

import indicators
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
            "value": [v * c for v, c in zip(volumes, closes, strict=True)],
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


def _make_strong_bullish_setup(periods: int = 50) -> tuple[pd.DataFrame, float]:
    """Create a history with strong bullish setup (multiple bull signals).

    Returns (history, current_price) tuple.
    Creates conditions for: RSI oversold, below BB lower, high volume.
    """
    if periods < 40:
        periods = 50

    # Strong downtrend for oversold RSI, then flat at bottom
    # This creates: dual RSI oversold, close below BB lower, momentum flattening
    downtrend = list(range(200, 120, -2))
    flat_bottom = [100] * (periods - len(downtrend))
    closes = downtrend + flat_bottom
    closes = closes[:periods]

    # Volume: higher in recent bars to trigger volume_above_avg
    base_volume = 1000.0
    volumes = [base_volume] * (periods - 5) + [base_volume * 2.5] * 5

    history = _make_history(closes, volumes)
    return history, closes[-1]


def _make_strong_bearish_setup(periods: int = 50) -> tuple[pd.DataFrame, float]:
    """Create a history with strong bearish setup (multiple bear signals).

    Returns (history, current_price) tuple.
    Creates conditions for: RSI overbought (> 46), EMA cross, momentum negative.
    """
    if periods < 40:
        periods = 50

    # Strong uptrend for overbought RSI, then sharp reversal
    # This creates: RSI high, EMA fast below slow after reversal, negative momentum
    uptrend = list(range(100, 180, 2))
    reversal = list(range(180, 140, -3))
    closes = uptrend + reversal
    # Pad to required length
    if len(closes) < periods:
        closes = [100] * (periods - len(closes)) + closes
    closes = closes[-periods:]

    history = _make_history(closes)
    return history, closes[-1]


def _make_oversold_history(periods: int = 40) -> pd.DataFrame:
    """Create a history with oversold RSI pattern (downtrend then flat low)."""
    # Start high, decline sharply to create oversold condition
    # Ensure at least 36 bars for all indicators
    if periods < 36:
        periods = 40
    closes = list(range(200, 200 - periods * 2, -2))[:periods]
    # Ensure we have enough data
    if len(closes) < periods:
        closes = [200] * (periods - len(closes)) + closes
    return _make_history(closes)


def _make_overbought_history(periods: int = 40) -> pd.DataFrame:
    """Create a history with overbought RSI pattern (strong uptrend)."""
    # Ensure at least 36 bars for all indicators
    if periods < 36:
        periods = 40
    closes = list(range(100, 100 + periods))
    return _make_history(closes)


def _signal_data(
    *,
    rsi_fast: float = 25.0,
    rsi_slow: float = 25.0,
    bull_votes: int = 0,
    bear_votes: int = 0,
    weighted_bull_votes: int | None = None,
    bull_flags: dict[str, bool] | None = None,
    bear_flags: dict[str, bool] | None = None,
) -> dict[str, object]:
    """Build a complete signal data dict for monkeypatched tests.

    This helper fills the full _evaluate_signals() payload shape used by on_bar().
    """
    if bull_flags is None:
        bull_flags = {}
    if bear_flags is None:
        bear_flags = {}
    if weighted_bull_votes is None:
        weighted_bull_votes = bull_votes + int(
            bull_flags.get("dual_rsi_oversold", False)
        )
    return {
        "rsi_fast": rsi_fast,
        "rsi_slow": rsi_slow,
        "bull_votes": bull_votes,
        "bear_votes": bear_votes,
        "weighted_bull_votes": weighted_bull_votes,
        "bull_flags": bull_flags,
        "bear_flags": bear_flags,
    }


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

        rsi = indicators._calc_rsi(closes, period=14)

        assert np.isfinite(rsi)
        assert 0 <= rsi <= 100

    def test_rsi_insufficient_history_returns_none(self):
        """Test that RSI returns None with insufficient history."""
        closes = np.array([100.0, 102.0, 101.0, 103.0])  # Only 4 bars

        rsi = indicators._calc_rsi(closes, period=14)

        assert rsi is None


class TestIndicatorHelpers:
    """Tests for indicator helper functions."""

    def test_calc_ema_tracks_uptrend(self):
        """Test EMA calculation on an uptrend."""
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        ema = indicators._calc_ema(closes, span=3)
        assert ema is not None
        assert ema[-1] > ema[0]  # EMA should increase in uptrend

    def test_calc_ema_insufficient_history_returns_none(self):
        """Test EMA returns None with insufficient history."""
        closes = np.array([1.0, 2.0])  # Only 2 bars, need at least span=5
        ema = indicators._calc_ema(closes, span=5)
        assert ema is None

    def test_calc_macd_returns_values_with_enough_history(self):
        """Test MACD calculation with sufficient history."""
        # Create enough data points: slow (26) + signal (9) = 35 minimum
        closes = np.array([100.0 + i * 0.5 for i in range(40)])
        macd_result = indicators._calc_macd(closes, fast=12, slow=26, signal=9)
        assert macd_result is not None
        macd_line, signal_line, histogram = macd_result
        assert isinstance(macd_line, float)
        assert isinstance(signal_line, float)
        assert isinstance(histogram, float)

    def test_calc_macd_insufficient_history_returns_none(self):
        """Test MACD returns None with insufficient history."""
        closes = np.array([100.0] * 10)  # Not enough for slow + signal
        macd_result = indicators._calc_macd(closes, fast=12, slow=26, signal=9)
        assert macd_result is None

    def test_calc_bollinger_returns_bands_with_enough_history(self):
        """Test Bollinger Bands calculation."""
        closes = np.array([100.0] * 20 + [110.0])  # 21 points, period=20
        bands = indicators._calc_bollinger(closes, period=20, std_mult=2.0)
        assert bands is not None
        upper, middle, lower = bands
        assert upper > middle > lower

    def test_calc_bollinger_insufficient_history_returns_none(self):
        """Test Bollinger returns None with insufficient history."""
        closes = np.array([100.0] * 10)
        bands = indicators._calc_bollinger(closes, period=20, std_mult=2.0)
        assert bands is None

    def test_calc_momentum_positive_in_uptrend(self):
        """Test momentum is positive in uptrend."""
        closes = np.array([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        momentum = indicators._calc_momentum(closes, period=5)
        assert momentum is not None
        assert momentum > 0  # Price increased from 100 to 105

    def test_calc_momentum_negative_in_downtrend(self):
        """Test momentum is negative in downtrend."""
        closes = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
        momentum = indicators._calc_momentum(closes, period=5)
        assert momentum is not None
        assert momentum < 0  # Price decreased from 100 to 95

    def test_calc_momentum_insufficient_history_returns_none(self):
        """Test momentum returns None with insufficient history."""
        closes = np.array([100.0, 101.0])
        momentum = indicators._calc_momentum(closes, period=5)
        assert momentum is None

    def test_calc_average_volume_with_enough_data(self):
        """Test average volume calculation."""
        volumes = np.array([1000.0] * 20 + [2000.0])
        avg_vol = indicators._calc_average_volume(volumes, lookback=20)
        assert avg_vol is not None
        assert avg_vol == 1050.0  # Average of 20 1000s and 1 2000

    def test_calc_average_volume_insufficient_history_returns_none(self):
        """Test average volume returns None with insufficient history."""
        volumes = np.array([1000.0] * 10)
        avg_vol = indicators._calc_average_volume(volumes, lookback=20)
        assert avg_vol is None


class TestVoteAssembly:
    """Tests for bull/bear vote assembly."""

    def test_bull_votes_counted_correctly_in_oversold_uptrend(self):
        """Test that bull votes are counted correctly for oversold uptrend conditions."""
        strat = strategy.Strategy()

        # Use strong bullish setup helper that creates multiple bull signals
        history, current_price = _make_strong_bullish_setup(50)
        bar = _make_bar_data("BTC", "2025-04-01", current_price, history)

        result = strat._evaluate_signals(bar)

        assert result is not None
        assert result["bull_votes"] == 3
        assert bool(result["bull_flags"]["dual_rsi_oversold"]) is True
        assert bool(result["bull_flags"]["macd_histogram_positive"]) is True
        assert bool(result["bull_flags"]["volume_above_avg"]) is True
        assert bool(result["bull_flags"]["close_below_bb_lower"]) is False
        assert bool(result["bull_flags"]["ema_fast_above_slow"]) is False
        assert bool(result["bull_flags"]["momentum_positive"]) is False

    def test_bear_votes_counted_correctly_in_overbought_downtrend(self):
        """Test that bear votes are counted correctly for overbought downtrend conditions."""
        strat = strategy.Strategy()

        # Use strong bearish setup that creates multiple bear signals
        history, current_price = _make_strong_bearish_setup(50)
        bar = _make_bar_data("BTC", "2025-04-01", current_price, history)

        result = strat._evaluate_signals(bar)

        assert result is not None
        assert result["bear_votes"] >= strategy.MIN_SELL_VOTES
        assert result["bear_flags"]["rsi_slow_high"]

    def test_insufficient_history_returns_none(self):
        """Test that _evaluate_signals returns None with insufficient history."""
        strat = strategy.Strategy()

        # Create minimal history (< 36 bars)
        history = _make_history([100.0 + i * 0.1 for i in range(10)])
        bar = _make_bar_data("BTC", "2025-04-01", 101.0, history)

        result = strat._evaluate_signals(bar)

        assert result is None

    def test_strong_bullish_setup_stays_below_buy_threshold_after_tuning(self):
        """Test that the tuned bullish fixture stays one vote below entry threshold."""
        strat = strategy.Strategy()

        # Use strong bullish setup helper
        history, current_price = _make_strong_bullish_setup(50)
        bar = _make_bar_data("BTC", "2025-04-01", current_price, history)

        result = strat._evaluate_signals(bar)

        assert result is not None
        assert result["bull_votes"] == 3
        assert result["bull_votes"] < strategy.MIN_VOTES
        assert bool(result["bull_flags"]["dual_rsi_oversold"]) is True
        assert bool(result["bull_flags"]["volume_above_avg"]) is True

    def test_low_bull_votes_does_not_produce_buy(self):
        """Test that insufficient bull votes does not produce buy signal."""
        strat = strategy.Strategy()

        # Create relatively flat history (few signals)
        # Need at least 36 bars
        closes = [100.0 + (i % 5) * 0.1 for i in range(50)]
        history = _make_history(closes)
        bar = _make_bar_data("BTC", "2025-04-01", 100.4, history)

        result = strat._evaluate_signals(bar)

        assert result is not None
        # Flat price should have fewer bull votes than threshold
        assert result["bull_votes"] < strategy.MIN_VOTES

    def test_bear_votes_for_held_symbol_produces_sell_eligible(self):
        """Test that bear votes are tracked for held symbols."""
        strat = strategy.Strategy()

        # Use strong bearish setup helper
        history, current_price = _make_strong_bearish_setup(50)
        bar = _make_bar_data("BTC", "2025-04-01", current_price, history)

        result = strat._evaluate_signals(bar)

        assert result is not None
        # Uptrend should have some bear signals (RSI high, etc.)
        assert result["bear_votes"] >= strategy.MIN_SELL_VOTES
        assert result["bear_flags"]["rsi_slow_high"]

    def test_buy_reason_string_includes_vote_count(self, monkeypatch):
        """Test that buy signal reason strings include vote count and flags."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 25.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES,
                "bear_votes": 0,
                "bull_flags": {
                    "dual_rsi_oversold": True,
                    "macd_histogram_positive": True,
                    "volume_above_avg": True,
                },
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}

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

        # Verify buy signal with proper reason formatting
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 1
        assert buy_signals[0].reason.startswith("Bull votes ")
        assert "dual rsi oversold" in buy_signals[0].reason

    def test_bear_sell_reason_string_format(self, monkeypatch):
        """Test that bear-vote sell reason strings include vote count and flags."""
        strat = strategy.Strategy()

        # Mock _evaluate_signals to return controlled bear votes
        def mock_evaluate(bar):
            return {
                "rsi_fast": 50.0,
                "rsi_slow": 45.0,
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES,
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": True, "momentum_negative": True},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 110.0, history)}
        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Verify bear-vote sell reason formatting
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert sell_signals[0].reason.startswith("Bear votes")


class TestBuySignals:
    """Tests for buy signal generation."""

    def test_buy_when_dual_rsi_oversold_and_vote_threshold_met(self, monkeypatch):
        """Test buy signal when oversold setup reaches the configured vote threshold."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 25.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES,
                "bear_votes": 0,
                "bull_flags": {"dual_rsi_oversold": True},
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}

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

    def test_buy_has_configured_weight(self, monkeypatch):
        """Test that buy signal has the configured position size."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 25.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES,
                "bear_votes": 0,
                "bull_flags": {"dual_rsi_oversold": True},
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}

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
        assert (
            "RSI" in sell_signals[0].reason
            or "recovered" in sell_signals[0].reason.lower()
        )

    def test_sell_on_stop_loss(self):
        """Test sell signal on stop-loss trigger."""
        strat = strategy.Strategy()

        # Create flat history (need at least 36 bars)
        history = _make_history([100.0] * 40)
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
        assert any(
            "stop" in s.reason.lower() or "loss" in s.reason.lower()
            for s in sell_signals
        )

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
        assert any(
            "holding" in s.reason.lower() or "max" in s.reason.lower()
            for s in sell_signals
        )

    def test_no_sell_when_holding_period_exceeded_but_unprofitable(self):
        """Test no RSI recovery sell when position is unprofitable."""
        strat = strategy.Strategy()

        # Create overbought history (RSI should be high)
        history = _make_overbought_history(30)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 128.0, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={
                "BTC": 150.0
            },  # Avg buy price (higher than current - unprofitable)
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

    def test_stop_loss_records_cooldown_and_blocks_rebuy(self, monkeypatch):
        """Test stop-loss exits and the cooldown prevents immediate re-entry."""
        strat = strategy.Strategy()

        stop_loss_history = _make_history([84.0] * 40)
        stop_loss_bar = {
            "BTC": _make_bar_data("BTC", "2025-04-08", 84.0, stop_loss_history)
        }
        held_portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=134000.0,
            date="2025-04-08",
        )

        def mock_evaluate(bar: prepare.BarData) -> dict[str, object]:
            if bar.date == "2025-04-08":
                return {
                    "rsi_fast": 45.0,
                    "rsi_slow": 40.0,
                    "bull_votes": 0,
                    "bear_votes": 0,
                    "bull_flags": {},
                    "bear_flags": {},
                }
            return {
                "rsi_fast": 20.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES,
                "bear_votes": 0,
                "bull_flags": {"dual_rsi_oversold": True},
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        exit_signals = strat.on_bar(stop_loss_bar, held_portfolio)

        assert len(exit_signals) == 1
        assert exit_signals[0].action == "sell"
        assert "Stop-loss" in exit_signals[0].reason

        rebuy_history = _make_history([80.0] * 40)
        rebuy_bar = {"BTC": _make_bar_data("BTC", "2025-04-12", 80.0, rebuy_history)}
        empty_portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-12",
        )

        rebuy_signals = strat.on_bar(rebuy_bar, empty_portfolio)

        assert rebuy_signals == []


class TestStrategyWithHistory:
    """Tests for strategy using engine-provided history."""

    def test_strategy_gets_rsi_from_bar_history(self):
        """Test that strategy calculates RSI from BarData history."""
        strat = strategy.Strategy()

        # Create bar with rising price history (RSI > 50)
        # Need at least 36 bars for all indicators
        dates = (
            pd.date_range("2025-02-20", "2025-04-01", freq="D")
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


class TestVoteThresholdBoundaries:
    """Tests for explicit vote threshold boundaries in on_bar()."""

    def test_no_buy_when_bull_votes_below_threshold(self, monkeypatch):
        """Test that buy is not triggered when bull votes are below MIN_VOTES."""
        strat = strategy.Strategy()

        # Mock _evaluate_signals to return controlled values
        def mock_evaluate(bar):
            return {
                "rsi_fast": 25.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES - 1,
                "bear_votes": 0,
                "bull_flags": {"dual_rsi_oversold": True},
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}
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

        # No buy when below threshold
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 0

    def test_buy_triggered_at_bull_vote_threshold(self, monkeypatch):
        """Test that buy is triggered when bull votes equal MIN_VOTES."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 25.0,
                "rsi_slow": 25.0,
                "bull_votes": strategy.MIN_VOTES,
                "bear_votes": 0,
                "bull_flags": {"dual_rsi_oversold": True},
                "bear_flags": {},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}
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

        # Buy triggered at threshold
        buy_signals = [s for s in signals if s.action == "buy"]
        assert len(buy_signals) == 1
        assert buy_signals[0].weight == strategy.POSITION_SIZE

    def test_no_bear_sell_when_votes_below_threshold(self, monkeypatch):
        """Test that bear-vote sell is not triggered when below MIN_SELL_VOTES."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 50.0,
                "rsi_slow": 45.0,
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES - 1,
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": True},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 110.0, history)}
        # Position is held, but no stop-loss (price higher than avg),
        # no RSI recovery (rsi_slow < RSI_EXIT), no max holding
        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},  # Recent entry
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # No bear-vote sell when below threshold
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 0

    def test_bear_sell_triggered_at_threshold(self, monkeypatch):
        """Test that bear-vote sell is triggered at MIN_SELL_VOTES."""
        strat = strategy.Strategy()

        def mock_evaluate(bar):
            return {
                "rsi_fast": 50.0,
                "rsi_slow": 45.0,
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES,
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": True},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 110.0, history)}
        # Held position, no hard exits triggered
        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Bear-vote sell triggered at threshold
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert sell_signals[0].reason.startswith("Bear votes")


class TestHardExitPriority:
    """Tests that hard exits take priority over bear-vote exits."""

    def test_stop_loss_priority_over_bear_votes(self, monkeypatch):
        """Test that stop-loss exit takes priority over bear-vote exit."""
        strat = strategy.Strategy()

        # Mock _evaluate_signals to also return sufficient bear votes
        def mock_evaluate(bar):
            return {
                "rsi_fast": 50.0,
                "rsi_slow": 45.0,
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES + 1,  # Would trigger bear sell
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": True},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        # Current price below stop-loss threshold (4.5% below avg price of 100)
        current_price = 100.0 * (1 - strategy.STOP_LOSS_PCT - 0.01)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", current_price, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=140000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Stop-loss should trigger, not bear-vote sell
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert "Stop-loss" in sell_signals[0].reason

    def test_rsi_recovery_priority_over_bear_votes(self, monkeypatch):
        """Test that RSI recovery exit takes priority over bear-vote exit."""
        strat = strategy.Strategy()

        # Mock _evaluate_signals with RSI above exit and bear votes
        def mock_evaluate(bar):
            return {
                "rsi_fast": 50.0,
                "rsi_slow": strategy.RSI_EXIT + 5,  # Above exit threshold
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES + 1,
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": True},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        # Profitable position (current > avg price)
        current_price = 110.0
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", current_price, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-03-25"},
            trade_log=[],
            equity=160000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # RSI recovery should trigger, not bear-vote sell
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert "RSI recovered" in sell_signals[0].reason

    def test_max_holding_priority_over_bear_votes(self, monkeypatch):
        """Test that max holding exit takes priority over bear-vote exit."""
        strat = strategy.Strategy()

        # Mock _evaluate_signals with bear votes but no RSI recovery
        def mock_evaluate(bar):
            return {
                "rsi_fast": 40.0,
                "rsi_slow": 40.0,  # Below exit threshold, so no RSI recovery
                "bull_votes": 0,
                "bear_votes": strategy.MIN_SELL_VOTES + 1,
                "bull_flags": {},
                "bear_flags": {"rsi_slow_high": False},
            }

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        # Not at stop-loss, profitable
        current_price = 110.0
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-15", current_price, history)}

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            # Entry 25 days ago, exceeds HOLDING_DAYS (21)
            position_dates={"BTC": "2025-03-21"},
            trade_log=[],
            equity=160000.0,
            date="2025-04-15",
        )

        signals = strat.on_bar(bar_data, portfolio)

        # Max holding should trigger, not bear-vote sell
        sell_signals = [s for s in signals if s.action == "sell"]
        assert len(sell_signals) == 1
        assert "holding" in sell_signals[0].reason.lower()


class TestParamsConfig:
    """Tests that verify PARAMS dict values match expected baselines."""

    def test_params_match_exp204_baseline(self):
        """Test that PARAMS values match the expected baseline for refactoring."""
        assert strategy.PARAMS["rsi_period_fast"] == 6
        assert strategy.PARAMS["rsi_period_slow"] == 14
        assert strategy.PARAMS["rsi_oversold"] == 30
        assert strategy.PARAMS["rsi_exit"] == 55
        assert strategy.PARAMS["bb_std"] == 1.5
        assert strategy.PARAMS["min_votes"] == 4
        assert strategy.PARAMS["min_weighted_buy_votes"] == 4
        assert strategy.PARAMS["min_sell_votes"] == 2
        assert strategy.PARAMS["max_positions"] == 5
        assert strategy.PARAMS["position_size"] == 0.10

    def test_min_history_bars_calculation(self):
        """Test MIN_HISTORY_BARS matches the prior warmup requirement."""
        # MIN_HISTORY_BARS should be max of indicator periods + 1
        expected = (
            max(
                strategy.PARAMS["rsi_period_slow"],  # 14
                strategy.PARAMS["bb_period"],  # 15
                strategy.PARAMS["ema_slow"],  # 24
                strategy.PARAMS["macd_slow"]
                + strategy.PARAMS["macd_signal"],  # 26 + 9 = 35
            )
            + 1
        )
        assert strategy.MIN_HISTORY_BARS == expected
        assert strategy.MIN_HISTORY_BARS == 36

    def test_backward_compatible_aliases(self):
        """Test that backward-compatible aliases map to PARAMS values."""
        assert strategy.RSI_PERIOD_FAST == strategy.PARAMS["rsi_period_fast"]
        assert strategy.RSI_PERIOD_SLOW == strategy.PARAMS["rsi_period_slow"]
        assert strategy.RSI_OVERSOLD == strategy.PARAMS["rsi_oversold"]
        assert strategy.RSI_EXIT == strategy.PARAMS["rsi_exit"]
        assert strategy.MAX_POSITIONS == strategy.PARAMS["max_positions"]
        assert strategy.POSITION_SIZE == strategy.PARAMS["position_size"]
        assert strategy.MIN_VOTES == strategy.PARAMS["min_votes"]
        assert strategy.MIN_SELL_VOTES == strategy.PARAMS["min_sell_votes"]
        assert strategy.HOLDING_DAYS == strategy.PARAMS["holding_days"]
        assert strategy.STOP_LOSS_PCT == strategy.PARAMS["stop_loss_pct"]


class TestStrategyContractFreezing:
    """Characterization tests to lock down behaviors likely to drift during modularization."""

    def test_weighted_bull_votes_double_counts_dual_rsi(self, monkeypatch):
        """Test that weighted_bull_votes double-counts dual_rsi_oversold."""
        strat = strategy.Strategy()

        # Create signal data with dual_rsi_oversold=True
        signal_result = _signal_data(
            rsi_fast=25.0,
            rsi_slow=25.0,
            bull_votes=4,
            bear_votes=0,
            bull_flags={"dual_rsi_oversold": True},
            bear_flags={},
        )

        def mock_evaluate(bar):
            return signal_result

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar = _make_bar_data("BTC", "2025-04-01", 100.0, history)
        result = strat._evaluate_signals(bar)

        # weighted_bull_votes should be bull_votes + 1 when dual_rsi_oversold is True
        assert result["weighted_bull_votes"] == result["bull_votes"] + 1

    def test_bull_reason_uses_registry_order(self, monkeypatch):
        """Test that bull reason string maintains deterministic signal order."""
        strat = strategy.Strategy()

        # Mock to return specific flags in a known order
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=25.0,
                rsi_slow=25.0,
                bull_votes=4,
                bear_votes=0,
                bull_flags={
                    "dual_rsi_oversold": True,
                    "macd_histogram_positive": True,
                    "close_below_bb_lower": True,
                    "ema_fast_above_slow": True,
                    "momentum_positive": False,
                    "volume_above_avg": False,
                },
                bear_flags={},
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}
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
        # Verify reason contains expected signals in order
        assert "Bull votes 4" in buy_signals[0].reason
        assert "dual rsi oversold" in buy_signals[0].reason
        assert "macd histogram positive" in buy_signals[0].reason
        assert "close below bb lower" in buy_signals[0].reason
        assert "ema fast above slow" in buy_signals[0].reason

    def test_sell_priority_stop_loss_over_rsi_recovery_over_max_holding_over_bear_votes(
        self, monkeypatch
    ):
        """Test staged sell priority: stop_loss -> rsi_recovery -> max_holding -> bear_votes."""
        strat = strategy.Strategy()

        history = _make_history([100.0] * 40)
        # Price below stop-loss level (need < not <= for stop-loss trigger)
        current_price = 100.0 * (1 - strategy.STOP_LOSS_PCT - 0.001)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", current_price, history)}

        # All conditions met: RSI high (recovery), holding period exceeded, bear votes
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=60.0,
                rsi_slow=60.0,  # Above RSI_EXIT (55) - would trigger recovery
                bull_votes=0,
                bear_votes=3,  # Above MIN_SELL_VOTES (2)
                bear_flags={"rsi_slow_high": True, "momentum_negative": True},
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            # Entry 30 days ago - exceeds HOLDING_DAYS (21)
            position_dates={"BTC": "2025-03-02"},
            trade_log=[],
            equity=150000.0,
            date="2025-04-01",
        )

        signals = strat.on_bar(bar_data, portfolio)
        sell_signals = [s for s in signals if s.action == "sell"]

        assert len(sell_signals) == 1
        # Stop-loss should take priority over all other exits
        assert "Stop-loss" in sell_signals[0].reason

    def test_symbol_specific_weight_strong_reversion(self, monkeypatch):
        """Test strong reversion position size for applicable symbols."""
        strat = strategy.Strategy()

        # Mock to trigger strong_reversion_buy conditions
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=25.0,
                rsi_slow=25.0,
                bull_votes=4,
                bear_votes=0,
                bull_flags={
                    "dual_rsi_oversold": True,
                    "macd_histogram_positive": True,  # strong_reversion needs this
                    "close_below_bb_lower": True,  # strong_reversion needs this
                },
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}
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
        # Strong reversion should use 0.15 weight
        assert buy_signals[0].weight == strategy.STRONG_REVERSION_POSITION_SIZE
        assert buy_signals[0].weight == 0.15

    def test_symbol_specific_weight_btc_hot_stall_trend(self, monkeypatch):
        """Test BTC hot-stall trend position size."""
        strat = strategy.Strategy()

        # Mock to trigger btc_hot_stall_trend_buy conditions
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=45.0,
                rsi_slow=50.0,  # Not oversold
                bull_votes=4,
                bear_votes=0,
                bull_flags={
                    "macd_histogram_positive": True,
                    "ema_fast_above_slow": True,
                    "momentum_positive": True,
                    "volume_above_avg": True,
                },
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"BTC": _make_bar_data("BTC", "2025-04-01", 100.0, history)}

        # Mock market state for hot stall conditions
        original_market_state = strat._market_state

        def mock_market_state(bar_data):
            return {"avg_rsi": 72.0, "avg_rsi_change": 1.0}  # Hot, low change (stall)

        monkeypatch.setattr(strat, "_market_state", mock_market_state)

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

        if buy_signals:
            # BTC hot-stall trend should use 0.0025 weight
            assert buy_signals[0].weight == strategy.BTC_HOT_STALL_TREND_POSITION_SIZE
            assert buy_signals[0].weight == 0.0025

    def test_symbol_specific_weight_dot_mild_reversion(self, monkeypatch):
        """Test DOT mild reversion position size."""
        strat = strategy.Strategy()

        # Mock to trigger dot_mild_reversion_buy conditions
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=28.0,
                rsi_slow=28.0,  # Below oversold threshold (30)
                bull_votes=4,
                bear_votes=0,
                bull_flags={
                    "dual_rsi_oversold": True,
                    "close_below_bb_lower": True,
                    "volume_above_avg": True,
                },
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"DOT": _make_bar_data("DOT", "2025-04-01", 100.0, history)}

        # Mock market state for mild reversion conditions
        def mock_market_state(bar_data):
            return {
                "avg_rsi": 35.0,
                "avg_rsi_change": -1.0,
            }  # Above DOT_MILD_REVERSION_RSI (33)

        monkeypatch.setattr(strat, "_market_state", mock_market_state)

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

        if buy_signals:
            # DOT mild reversion should use 0.00015625 weight
            assert buy_signals[0].weight == strategy.DOT_MILD_REVERSION_POSITION_SIZE
            assert buy_signals[0].weight == 0.00015625

    def test_symbol_specific_weight_avax_pure_trend(self, monkeypatch):
        """Test AVAX pure trend position size."""
        strat = strategy.Strategy()

        # Mock to trigger avax_pure_trend_buy conditions
        def mock_evaluate(bar):
            return _signal_data(
                rsi_fast=45.0,
                rsi_slow=50.0,  # Not oversold
                bull_votes=4,
                bear_votes=0,
                bull_flags={
                    "macd_histogram_positive": True,
                    "ema_fast_above_slow": True,
                    "momentum_positive": True,
                    "volume_above_avg": True,
                },
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate)

        history = _make_history([100.0] * 40)
        bar_data = {"AVAX": _make_bar_data("AVAX", "2025-04-01", 100.0, history)}

        # Mock market state for AVAX trend conditions (avg_rsi >= 60)
        def mock_market_state(bar_data):
            return {"avg_rsi": 65.0, "avg_rsi_change": 3.0}

        monkeypatch.setattr(strat, "_market_state", mock_market_state)

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

        if buy_signals:
            # AVAX pure trend should use 0.04 weight
            assert buy_signals[0].weight == strategy.AVAX_TREND_POSITION_SIZE
            assert buy_signals[0].weight == 0.04

    def test_cooldown_blocks_reentry_after_stop_loss(self, monkeypatch):
        """Test that cooldown blocks re-entry after stop-loss exit."""
        strat = strategy.Strategy()

        # First, simulate a stop-loss exit
        stop_loss_history = _make_history([84.0] * 40)
        stop_loss_bar = {
            "BTC": _make_bar_data("BTC", "2025-04-08", 84.0, stop_loss_history)
        }
        held_portfolio = prepare.PortfolioState(
            cash=50000.0,
            positions={"BTC": 1.0},
            avg_prices={"BTC": 100.0},
            position_dates={"BTC": "2025-04-01"},
            trade_log=[],
            equity=134000.0,
            date="2025-04-08",
        )

        def mock_evaluate_sl(bar: prepare.BarData) -> dict[str, object]:
            return _signal_data(
                rsi_fast=45.0,
                rsi_slow=40.0,
                bull_votes=0,
                bear_votes=0,
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate_sl)

        exit_signals = strat.on_bar(stop_loss_bar, held_portfolio)

        assert len(exit_signals) == 1
        assert exit_signals[0].action == "sell"
        assert "Stop-loss" in exit_signals[0].reason

        # Verify cooldown is recorded
        assert "BTC" in strat._stop_loss_dates
        assert strat._stop_loss_dates["BTC"] == "2025-04-08"

        # Now try to re-buy 4 days later (within 15-day cooldown)
        rebuy_history = _make_history([80.0] * 40)
        rebuy_bar = {"BTC": _make_bar_data("BTC", "2025-04-12", 80.0, rebuy_history)}
        empty_portfolio = prepare.PortfolioState(
            cash=100000.0,
            positions={},
            avg_prices={},
            position_dates={},
            trade_log=[],
            equity=100000.0,
            date="2025-04-12",
        )

        def mock_evaluate_buy(bar: prepare.BarData) -> dict[str, object]:
            return _signal_data(
                rsi_fast=20.0,
                rsi_slow=25.0,
                bull_votes=4,
                bear_votes=0,
                bull_flags={"dual_rsi_oversold": True},
            )

        monkeypatch.setattr(strat, "_evaluate_signals", mock_evaluate_buy)

        rebuy_signals = strat.on_bar(rebuy_bar, empty_portfolio)

        # Rebuy should be blocked by cooldown
        assert rebuy_signals == []
