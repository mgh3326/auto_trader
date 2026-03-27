"""RSI indicator using Wilder's smoothing method."""

import numpy as np


def calc_rsi(closes: np.ndarray, period: int = 14) -> float | None:
    """Calculate current RSI value for a price series.

    Uses Wilder's exponential smoothing (same as TradingView).

    Args:
        closes: Array of closing prices, oldest first.
        period: RSI lookback period.

    Returns:
        RSI value (0-100) or None if insufficient data.
    """
    if len(closes) < period + 1:
        return None

    deltas = np.diff(closes)

    # Seed averages with SMA of first `period` deltas
    seed = deltas[:period]
    avg_gain = np.where(seed > 0, seed, 0.0).sum() / period
    avg_loss = -np.where(seed < 0, seed, 0.0).sum() / period

    # Wilder's smoothing for remaining deltas
    for delta in deltas[period:]:
        if delta > 0:
            avg_gain = (avg_gain * (period - 1) + delta) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) - delta) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate RSI for every point in the series.

    Args:
        closes: Array of closing prices, oldest first.
        period: RSI lookback period.

    Returns:
        Array of RSI values (NaN where insufficient data).
    """
    n = len(closes)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed with SMA
    avg_gain = gains[:period].sum() / period
    avg_loss = losses[:period].sum() / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    # Wilder's smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    return rsi
