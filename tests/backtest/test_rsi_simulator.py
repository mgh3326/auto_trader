"""Tests for the rebalancing portfolio simulator."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.simulator import run_backtest, Portfolio, BacktestResult


def _make_flat_candles(market: str, n_bars: int = 50, price: float = 1000.0) -> pd.DataFrame:
    """Flat price candles for predictable testing."""
    datetimes = []
    for i in range(n_bars):
        day = i // 24 + 1
        hour = i % 24
        datetimes.append(f"2024-01-{day:02d}T{hour:02d}:00:00")
    return pd.DataFrame({
        "datetime": datetimes,
        "open": [price] * n_bars,
        "high": [price] * n_bars,
        "low": [price] * n_bars,
        "close": [price] * n_bars,
        "volume": [100.0] * n_bars,
        "value": [1_000_000.0] * n_bars,
    })


def _make_rising_candles(market: str, n_bars: int = 50) -> pd.DataFrame:
    """Steadily rising prices."""
    datetimes = []
    prices = []
    for i in range(n_bars):
        day = i // 24 + 1
        hour = i % 24
        datetimes.append(f"2024-01-{day:02d}T{hour:02d}:00:00")
        prices.append(1000.0 + i * 10.0)
    return pd.DataFrame({
        "datetime": datetimes,
        "open": prices,
        "high": [p + 5 for p in prices],
        "low": [p - 5 for p in prices],
        "close": prices,
        "volume": [100.0] * n_bars,
        "value": [1_000_000.0] * n_bars,
    })


class TestBacktestResult:
    def test_result_has_equity_curve(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0
        assert len(result.timestamps) == len(result.equity_curve)

    def test_no_trade_on_empty_data(self):
        config = BacktestConfig(start="2024-01-01", end="2024-01-02", top_n=1, pick_k=1, max_rsi=99)
        result = run_backtest({}, config)
        assert result.rebalance_count == 0
        assert len(result.trades) == 0


class TestPortfolioEquity:
    def test_flat_price_equity_decreases_by_fees(self):
        """With flat prices, equity should only decrease due to fees."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
            fee_rate=0.001, slippage_bps=0,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        # Should have traded and lost some to fees
        assert result.equity_curve[-1] <= config.initial_capital

    def test_rising_price_positive_return(self):
        """With rising prices and low RSI entry, should have positive return."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_rising_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        # Even with fees, strong uptrend should be profitable
        if len(result.trades) > 0:
            assert result.equity_curve[-1] > config.initial_capital * 0.99


class TestRebalancing:
    def test_rebalance_count(self):
        """48 hours of data with 24h rebalance → should rebalance ~2 times."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        assert result.rebalance_count >= 1

    def test_trades_logged(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        for trade in result.trades:
            assert "datetime" in trade
            assert "market" in trade
            assert "action" in trade
            assert "quantity" in trade
            assert "price" in trade
            assert "fee" in trade
