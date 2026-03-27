"""Dynamic universe selection by rolling trade value."""

import pandas as pd


def select_universe(
    all_data: dict[str, pd.DataFrame],
    timestamp: str,
    top_n: int,
    window: int = 24,
) -> list[str]:
    """Select top-N markets by rolling trade value at a given timestamp.

    Args:
        all_data: Dict mapping market code to 1h candle DataFrame.
        timestamp: ISO datetime string "YYYY-MM-DDTHH:MM:SS".
        top_n: Number of markets to select.
        window: Rolling window size in hours for trade value sum.

    Returns:
        List of market codes sorted by descending rolling trade value.
    """
    scores: list[tuple[str, float]] = []

    for market, df in all_data.items():
        # Find rows up to and including the timestamp
        mask = df["datetime"] <= timestamp
        subset = df[mask]
        if len(subset) == 0:
            continue

        # Rolling sum of trade value over the window
        tail = subset.tail(window)
        rolling_value = tail["value"].sum()
        scores.append((market, rolling_value))

    # Sort descending by value, take top N
    scores.sort(key=lambda x: x[1], reverse=True)
    return [market for market, _ in scores[:top_n]]
