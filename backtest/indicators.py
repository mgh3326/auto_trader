"""Shared indicator helpers for backtest strategies."""

import numpy as np
import pandas as pd


def _calc_rsi(closes: np.ndarray, period: int) -> float | None:
    """Calculate RSI using Wilder's smoothing method."""
    if len(closes) < period + 1:
        return None

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    rsi = 100 - (100 / (1 + rs))
    return float(rsi)


def _calc_ema(closes: np.ndarray, span: int) -> np.ndarray | None:
    """Calculate EMA using exponential smoothing.

    Returns None if insufficient history (need at least span data points).
    Returns full EMA array; caller should use [-1] for latest value.
    """
    if len(closes) < span:
        return None
    return pd.Series(closes).ewm(span=span, adjust=False).mean().values


def _calc_macd(
    closes: np.ndarray, fast: int, slow: int, signal: int
) -> tuple[float, float, float] | None:
    """Calculate MACD line, signal line, and histogram.

    Returns (macd_line, signal_line, histogram) or None if insufficient history.
    """
    if len(closes) < slow + signal:
        return None
    ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean()
    ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean()
    # NOSONAR python:S5607 — Sonar mis-types pandas Series subtraction.
    macd_line = ema_fast - ema_slow  # NOSONAR
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line  # NOSONAR python:S5607
    return (
        float(macd_line.iloc[-1]),
        float(signal_line.iloc[-1]),
        float(histogram.iloc[-1]),
    )


def _calc_bollinger(
    closes: np.ndarray, period: int, std_mult: float
) -> tuple[float, float, float] | None:
    """Calculate Bollinger Bands (upper, middle, lower).

    Returns (upper, middle, lower) or None if insufficient history.
    """
    if len(closes) < period:
        return None
    middle = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    return float(upper), float(middle), float(lower)


def _calc_momentum(closes: np.ndarray, period: int) -> float | None:
    """Calculate price momentum: (current - period_ago) / period_ago * 100.

    Returns momentum value or None if insufficient history.
    """
    if len(closes) < period + 1:
        return None
    current = closes[-1]
    past = closes[-(period + 1)]
    return float((current - past) / past * 100)


def _calc_average_volume(volumes: np.ndarray, lookback: int) -> float | None:
    """Calculate average volume over lookback period.

    Returns average volume or None if insufficient history.
    """
    if len(volumes) < lookback:
        return None
    return float(np.mean(volumes[-lookback:]))
