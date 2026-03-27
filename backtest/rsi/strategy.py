"""RSI-based coin selection strategy."""

import pandas as pd

from .config import BacktestConfig
from .indicators import calc_rsi


def select_coins(
    universe: list[str],
    all_data: dict[str, pd.DataFrame],
    timestamp: str,
    config: BacktestConfig,
) -> list[str]:
    """Select coins from universe by RSI ascending, filtered by max_rsi.

    Args:
        universe: Candidate market codes (pre-filtered by trade value).
        all_data: Dict mapping market code to 1h candle DataFrame.
        timestamp: Current rebalance timestamp.
        config: Backtest configuration.

    Returns:
        List of selected market codes, sorted by RSI ascending.
    """
    candidates: list[tuple[str, float]] = []

    for market in universe:
        df = all_data.get(market)
        if df is None:
            continue

        # Get data up to timestamp
        mask = df["datetime"] <= timestamp
        subset = df[mask]
        if len(subset) < config.rsi_period + 1:
            continue

        closes = subset["close"].to_numpy(dtype=float)
        rsi = calc_rsi(closes, period=config.rsi_period)
        if rsi is None:
            continue

        if rsi <= config.max_rsi:
            candidates.append((market, rsi))

    # Sort by RSI ascending (lowest RSI first)
    candidates.sort(key=lambda x: x[1])

    # Pick top K
    return [market for market, _ in candidates[: config.pick_k]]
