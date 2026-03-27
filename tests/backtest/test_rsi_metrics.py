"""Tests for performance metrics calculation."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.metrics import compute_metrics, Metrics
from rsi.simulator import BacktestResult


def _make_result(equity_curve: list[float], n_trades: int = 0) -> BacktestResult:
    """Helper to build a BacktestResult for metric tests."""
    n = len(equity_curve)
    timestamps = [f"2024-01-01T{i:02d}:00:00" for i in range(n)]
    trades = [{"action": "buy", "market": "KRW-BTC", "quantity": 1, "price": 100, "fee": 0.05, "datetime": timestamps[0]}] * n_trades
    config = BacktestConfig(start="2024-01-01", end="2024-01-02")
    return BacktestResult(
        equity_curve=equity_curve,
        timestamps=timestamps,
        trades=trades,
        rebalance_count=1,
        config=config,
    )


class TestCumulativeReturn:
    def test_positive_return(self):
        result = _make_result([10_000_000, 11_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(0.10, abs=0.001)

    def test_negative_return(self):
        result = _make_result([10_000_000, 9_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(-0.10, abs=0.001)

    def test_no_change(self):
        result = _make_result([10_000_000, 10_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(0.0)


class TestMaxDrawdown:
    def test_no_drawdown(self):
        result = _make_result([100, 110, 120, 130])
        m = compute_metrics(result)
        assert m.max_drawdown == pytest.approx(0.0)

    def test_known_drawdown(self):
        # Peak at 200, trough at 100 → 50% drawdown
        result = _make_result([100, 200, 100, 150])
        m = compute_metrics(result)
        assert m.max_drawdown == pytest.approx(0.50, abs=0.01)


class TestSharpe:
    def test_flat_equity_zero_sharpe(self):
        result = _make_result([100, 100, 100, 100])
        m = compute_metrics(result)
        assert m.sharpe == 0.0

    def test_positive_sharpe_for_steady_gains(self):
        curve = [10_000_000 + i * 10_000 for i in range(100)]
        result = _make_result(curve)
        m = compute_metrics(result)
        assert m.sharpe > 0


class TestTradeCount:
    def test_counts_trades(self):
        result = _make_result([100, 110], n_trades=5)
        m = compute_metrics(result)
        assert m.trade_count == 5


class TestBenchmark:
    def test_btc_benchmark(self):
        result = _make_result([10_000_000, 11_000_000])
        btc_data = pd.DataFrame({
            "datetime": ["2024-01-01T00:00:00", "2024-01-01T01:00:00"],
            "close": [50_000_000, 55_000_000],
        })
        m = compute_metrics(result, btc_data=btc_data)
        assert m.benchmark_return == pytest.approx(0.10, abs=0.001)

    def test_no_btc_data_returns_none(self):
        result = _make_result([10_000_000, 11_000_000])
        m = compute_metrics(result)
        assert m.benchmark_return is None
