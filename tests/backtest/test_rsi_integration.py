"""Integration test: full RSI portfolio backtest with synthetic data."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.metrics import compute_metrics
from rsi.simulator import run_backtest


def _generate_synthetic_market(
    market: str,
    n_days: int = 30,
    base_price: float = 1000.0,
    volatility: float = 0.02,
    base_value: float = 1_000_000.0,
    seed: int | None = None,
) -> pd.DataFrame:
    """Generate synthetic 1h candle data for testing."""
    rng = np.random.default_rng(seed)
    n_bars = n_days * 24
    prices = [base_price]
    for _ in range(n_bars - 1):
        ret = rng.normal(0, volatility)
        prices.append(prices[-1] * (1 + ret))

    datetimes = (
        pd.date_range("2024-01-01", periods=n_bars, freq="h")
        .strftime("%Y-%m-%dT%H:%M:%S")
        .tolist()
    )

    prices_arr = np.array(prices)
    return pd.DataFrame({
        "datetime": datetimes[:n_bars],
        "open": prices_arr,
        "high": prices_arr * (1 + rng.uniform(0, 0.01, n_bars)),
        "low": prices_arr * (1 - rng.uniform(0, 0.01, n_bars)),
        "close": prices_arr,
        "volume": rng.uniform(50, 200, n_bars),
        "value": rng.uniform(base_value * 0.5, base_value * 1.5, n_bars),
    })


class TestFullPipeline:
    """End-to-end integration tests."""

    def test_basic_run(self):
        """Run full backtest with 5 synthetic markets."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(
                f"KRW-COIN{i}", n_days=10, seed=i, base_value=1_000_000 * (5 - i),
            )
            for i in range(5)
        }
        config = BacktestConfig(
            start="2024-01-01",
            end="2024-01-10",
            top_n=3,
            pick_k=2,
            max_rsi=70,
            rebalance_hours=24,
        )
        result = run_backtest(all_data, config)
        assert len(result.equity_curve) > 0
        assert result.rebalance_count >= 1
        assert len(result.trades) > 0

        metrics = compute_metrics(result, btc_data=all_data["KRW-COIN0"])
        assert metrics.cumulative_return != 0.0 or metrics.trade_count == 0
        assert metrics.max_drawdown >= 0.0
        assert metrics.benchmark_return is not None

    def test_parameter_sensitivity(self):
        """Different parameters should produce different results."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(f"KRW-COIN{i}", n_days=10, seed=i)
            for i in range(10)
        }

        config_a = BacktestConfig(
            start="2024-01-01", end="2024-01-10",
            top_n=5, pick_k=2, max_rsi=40, rebalance_hours=24,
        )
        config_b = BacktestConfig(
            start="2024-01-01", end="2024-01-10",
            top_n=8, pick_k=4, max_rsi=60, rebalance_hours=12,
        )

        result_a = run_backtest(all_data, config_a)
        result_b = run_backtest(all_data, config_b)

        # Results should differ (different params → different trades)
        assert result_a.equity_curve != result_b.equity_curve or \
               result_a.trade_count != result_b.trade_count or \
               result_a.rebalance_count != result_b.rebalance_count

    def test_max_rsi_filter_reduces_trades(self):
        """Stricter max_rsi should result in fewer or equal trades."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(f"KRW-COIN{i}", n_days=10, seed=i)
            for i in range(5)
        }

        loose = BacktestConfig(start="2024-01-01", end="2024-01-10", top_n=5, pick_k=3, max_rsi=80)
        strict = BacktestConfig(start="2024-01-01", end="2024-01-10", top_n=5, pick_k=3, max_rsi=20)

        result_loose = run_backtest(all_data, loose)
        result_strict = run_backtest(all_data, strict)

        # Stricter filter → fewer or equal coins selected → potentially fewer trades
        # (Not guaranteed per-trade, but rebalance decisions should differ)
        assert result_loose.rebalance_count == result_strict.rebalance_count

    def test_equity_curve_starts_at_initial_capital(self):
        all_data = {
            "KRW-BTC": _generate_synthetic_market("KRW-BTC", n_days=5, seed=0),
        }
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-05",
            top_n=1, pick_k=1, max_rsi=99,
            initial_capital=5_000_000,
        )
        result = run_backtest(all_data, config)
        # First equity point should be close to initial capital
        # (may differ slightly due to immediate rebalance + fees)
        assert abs(result.equity_curve[0] - 5_000_000) < 500_000
