"""Tests for RSI-based coin selection strategy."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.indicators import calc_rsi
from rsi.strategy import select_coins


def _make_candles_with_rsi(
    market: str, rsi_target: float, n_bars: int = 30
) -> pd.DataFrame:
    """Create candle data that produces approximately the target RSI.

    Uses a simple approach: fixed base with controlled up/down moves.
    """
    rng = np.random.default_rng(hash(market) % 2**32)
    # Probability of up-move that yields target RSI (approx)
    p_up = rsi_target / 100.0
    base = 1000.0
    prices = [base]
    for _ in range(n_bars - 1):
        if rng.random() < p_up:
            prices.append(prices[-1] + rng.uniform(1, 5))
        else:
            prices.append(prices[-1] - rng.uniform(1, 5))

    datetimes = [
        f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00" for i in range(n_bars)
    ]
    return pd.DataFrame(
        {
            "datetime": datetimes,
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [100.0] * n_bars,
            "value": [100000.0] * n_bars,
        }
    )


class TestSelectCoins:
    def test_filters_by_max_rsi(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-02-01", max_rsi=30.0, pick_k=5, rsi_period=14
        )
        # Create one coin with very high RSI (all gains)
        high_rsi = pd.DataFrame(
            {
                "datetime": [f"2024-01-01T{h:02d}:00:00" for h in range(20)],
                "open": list(range(100, 120)),
                "high": list(range(101, 121)),
                "low": list(range(99, 119)),
                "close": list(range(100, 120)),
                "volume": [100.0] * 20,
                "value": [100000.0] * 20,
            }
        )
        all_data = {"KRW-HIGH": high_rsi}
        universe = ["KRW-HIGH"]
        timestamp = "2024-01-01T19:00:00"
        result = select_coins(universe, all_data, timestamp, config)
        # RSI should be ~100 → filtered out by max_rsi=30
        assert len(result) == 0

    def test_sorts_by_rsi_ascending(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-02-01", max_rsi=80.0, pick_k=3, rsi_period=14
        )
        all_data = {
            "KRW-A": _make_candles_with_rsi("KRW-A", 60),
            "KRW-B": _make_candles_with_rsi("KRW-B", 30),
            "KRW-C": _make_candles_with_rsi("KRW-C", 45),
        }
        universe = ["KRW-A", "KRW-B", "KRW-C"]
        ts = all_data["KRW-A"]["datetime"].iloc[-1]
        result = select_coins(universe, all_data, ts, config)
        assert len(result) > 0
        # Verify RSI ordering: each selected coin's RSI <= next one's
        rsi_values = []
        for market in result:
            closes = all_data[market][all_data[market]["datetime"] <= ts][
                "close"
            ].to_numpy(dtype=float)
            rsi_values.append(calc_rsi(closes, period=14))
        for i in range(len(rsi_values) - 1):
            assert rsi_values[i] <= rsi_values[i + 1], (
                f"RSI not ascending: {result[i]}={rsi_values[i]:.1f} > {result[i + 1]}={rsi_values[i + 1]:.1f}"
            )

    def test_picks_at_most_k(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-02-01", max_rsi=99.0, pick_k=2, rsi_period=14
        )
        all_data = {
            f"KRW-{c}": _make_candles_with_rsi(f"KRW-{c}", 40)
            for c in ["A", "B", "C", "D"]
        }
        universe = list(all_data.keys())
        ts = list(all_data.values())[0]["datetime"].iloc[-1]
        result = select_coins(universe, all_data, ts, config)
        assert len(result) <= 2

    def test_skips_coins_with_insufficient_data(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-02-01", max_rsi=80.0, pick_k=5, rsi_period=14
        )
        short = pd.DataFrame(
            {
                "datetime": ["2024-01-01T00:00:00", "2024-01-01T01:00:00"],
                "open": [100, 101],
                "high": [100, 101],
                "low": [100, 101],
                "close": [100, 101],
                "volume": [10, 10],
                "value": [1000, 1000],
            }
        )
        all_data = {"KRW-SHORT": short}
        result = select_coins(["KRW-SHORT"], all_data, "2024-01-01T01:00:00", config)
        assert len(result) == 0
