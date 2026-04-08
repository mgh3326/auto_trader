"""Crypto multi-signal voting system for live trading decisions.

Ported from backtest/strategy.py (PR #381, Sharpe 2.45).
Evaluates 6 bull signals and 5 bear signals from OHLCV data.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, cast

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Validated parameters from backtest PR #381
RSI_PERIOD_FAST = 7
RSI_PERIOD_SLOW = 14
RSI_OVERSOLD = 30
RSI_EXIT = 46

MIN_VOTES = 4
MIN_SELL_VOTES = 2

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 15
BB_STD = 2.0
EMA_FAST = 8
EMA_SLOW = 24
MOMENTUM_PERIOD = 5
VOLUME_LOOKBACK = 20
VOLUME_THRESHOLD = 1.5

# Minimum bars needed for all indicators
MIN_HISTORY_BARS = (
    max(RSI_PERIOD_SLOW, BB_PERIOD, EMA_SLOW, MACD_SLOW + MACD_SIGNAL) + 1
)


@dataclasses.dataclass(frozen=True)
class VotingResult:
    rsi_fast: float | None
    rsi_slow: float | None
    bull_votes: int
    bear_votes: int
    bull_flags: dict[str, bool]
    bear_flags: dict[str, bool]
    buy_signal: bool  # bull_votes >= MIN_VOTES
    sell_signal: bool  # bear_votes >= MIN_SELL_VOTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "rsi_fast": self.rsi_fast,
            "rsi_slow": self.rsi_slow,
            "bull_votes": self.bull_votes,
            "bear_votes": self.bear_votes,
            "bull_flags": self.bull_flags,
            "bear_flags": self.bear_flags,
            "buy_signal": self.buy_signal,
            "sell_signal": self.sell_signal,
        }


class CryptoVotingSignals:
    """Evaluate multi-signal voting for crypto positions/candidates."""

    def evaluate(self, df: pd.DataFrame) -> VotingResult | None:
        """Evaluate all signals from OHLCV DataFrame.

        Args:
            df: DataFrame with columns: open, high, low, close, volume.
                Must have at least MIN_HISTORY_BARS rows.

        Returns:
            VotingResult or None if insufficient data.
        """
        if df is None or len(df) < MIN_HISTORY_BARS:
            return None

        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float)
        current_close = closes[-1]
        current_volume = volumes[-1]

        # Calculate indicators
        rsi_fast = _calc_rsi(closes, RSI_PERIOD_FAST)
        rsi_slow = _calc_rsi(closes, RSI_PERIOD_SLOW)
        if rsi_slow is None:
            return None

        macd_result = _calc_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        bb_result = _calc_bollinger(closes, BB_PERIOD, BB_STD)
        ema_fast = _calc_ema(closes, EMA_FAST)
        ema_slow = _calc_ema(closes, EMA_SLOW)
        momentum = _calc_momentum(closes, MOMENTUM_PERIOD)
        avg_volume = _calc_average_volume(volumes, VOLUME_LOOKBACK)

        # Bull signals (6)
        bull_flags = {
            "dual_rsi_oversold": bool(
                rsi_slow <= RSI_OVERSOLD
                and rsi_fast is not None
                and rsi_fast <= RSI_OVERSOLD
            ),
            "macd_histogram_positive": bool(macd_result is not None and macd_result[2] > 0),
            "close_below_bb_lower": bool(
                bb_result is not None and current_close < bb_result[2]
            ),
            "ema_fast_above_slow": bool(
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] > ema_slow[-1]
            ),
            "momentum_positive": bool(momentum is not None and momentum > 0),
            "volume_above_avg": bool(
                avg_volume is not None
                and current_volume > avg_volume * VOLUME_THRESHOLD
            ),
        }
        bull_votes = sum(1 for v in bull_flags.values() if v)

        # Bear signals (5)
        bear_flags = {
            "macd_histogram_negative": bool(macd_result is not None and macd_result[2] < 0),
            "close_above_bb_upper": bool(
                bb_result is not None and current_close > bb_result[0]
            ),
            "ema_fast_below_slow": bool(
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] < ema_slow[-1]
            ),
            "momentum_negative": bool(momentum is not None and momentum < 0),
            "rsi_slow_high": bool(rsi_slow > RSI_EXIT),
        }
        bear_votes = sum(1 for v in bear_flags.values() if v)

        return VotingResult(
            rsi_fast=rsi_fast,
            rsi_slow=rsi_slow,
            bull_votes=bull_votes,
            bear_votes=bear_votes,
            bull_flags=bull_flags,
            bear_flags=bear_flags,
            buy_signal=bool(bull_votes >= MIN_VOTES),
            sell_signal=bool(bear_votes >= MIN_SELL_VOTES),
        )


# --- Indicator functions (ported from backtest/strategy.py) ---


def _calc_rsi(closes: np.ndarray, period: int) -> float | None:
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
    return float(100 - (100 / (1 + rs)))


def _calc_ema(closes: np.ndarray, span: int) -> np.ndarray | None:
    if len(closes) < span:
        return None
    return cast(
        np.ndarray, pd.Series(closes).ewm(span=span, adjust=False).mean().to_numpy()
    )


def _calc_macd(
    closes: np.ndarray, fast: int, slow: int, signal: int
) -> tuple[float, float, float] | None:
    if len(closes) < slow + signal:
        return None
    ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean()
    ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return (
        float(macd_line.iloc[-1]),
        float(signal_line.iloc[-1]),
        float(histogram.iloc[-1]),
    )


def _calc_bollinger(
    closes: np.ndarray, period: int, std_mult: float
) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    middle = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return float(middle + std_mult * std), float(middle), float(middle - std_mult * std)


def _calc_momentum(closes: np.ndarray, period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    return float((closes[-1] - closes[-(period + 1)]) / closes[-(period + 1)] * 100)


def _calc_average_volume(volumes: np.ndarray, lookback: int) -> float | None:
    if len(volumes) < lookback:
        return None
    return float(np.mean(volumes[-lookback:]))
