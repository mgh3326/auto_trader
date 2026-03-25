"""Backtest strategy implementation."""

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

import prepare

if TYPE_CHECKING:
    from pandas import Series

# Strategy Constants
RSI_PERIOD_FAST = 7
RSI_PERIOD_SLOW = 14
RSI_OVERSOLD = 30
RSI_EXIT = 55
MAX_POSITIONS = 5
POSITION_SIZE = 0.10
HOLDING_DAYS = 21
STOP_LOSS_PCT = 0.02
COOLDOWN_DAYS = 15

# Multi-Signal Voting Parameters
MIN_VOTES = 4
MIN_WEIGHTED_BUY_VOTES = 4
MIN_SELL_VOTES = 2
BLOCK_HIGH_RSI_BUYS = False
TOTAL_BULL_SIGNALS = 6  # Total number of possible bull signals for vote ratio
FALLING_MARKET_BLOCK_BUYS = True
FALLING_MARKET_RSI_LEVEL = 55.0
FALLING_MARKET_CHANGE = -1.0
EXTREME_FALLING_MARKET_CHANGE = -6.0
REVERSION_MARKET_CHANGE_CEILING = -2.0
OVERHEATED_MARKET_RSI_LEVEL = 75.0

# Indicator Periods
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 15
BB_STD = 1.5
EMA_FAST = 8
EMA_SLOW = 24
MOMENTUM_PERIOD = 5
VOLUME_LOOKBACK = 20
VOLUME_THRESHOLD = 1.5


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

    rs = avg_gain / avg_loss if avg_loss > 0 else float('inf')
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
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


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


def _format_vote_reason(prefix: str, votes: int, flags: dict[str, bool], limit: int) -> str:
    """Format vote reason string with triggered signal names.

    Args:
        prefix: 'Bull' or 'Bear' prefix
        votes: Number of votes
        flags: Dict of signal names to triggered status
        limit: Max number of signal names to include

    Returns:
        Formatted reason string like 'Bull votes 3/6: signal1, signal2'
    """
    total_signals = len(flags)
    triggered = [k.replace("_", " ") for k, v in flags.items() if v]
    return f"{prefix} votes {votes}/{total_signals}: {', '.join(triggered[:limit])}"


class Strategy:
    """Dual RSI mean-reversion with cooldown after stop-loss."""

    def __init__(self) -> None:
        self._stop_loss_dates: dict[str, str] = {}  # symbol -> date of stop-loss

    def _days_between(self, date1: str, date2: str) -> int:
        from datetime import datetime
        try:
            d1 = datetime.strptime(date1, "%Y-%m-%d")
            d2 = datetime.strptime(date2, "%Y-%m-%d")
            return (d2 - d1).days
        except (ValueError, TypeError):
            return 999

    def _market_state(
        self,
        bar_data: dict[str, prepare.BarData],
    ) -> dict[str, float]:
        current_rsis: list[float] = []
        previous_rsis: list[float] = []

        for bar in bar_data.values():
            closes = bar.history["close"].values
            current_rsi = _calc_rsi(closes, RSI_PERIOD_SLOW)
            if current_rsi is None:
                continue
            current_rsis.append(current_rsi)
            if len(closes) >= RSI_PERIOD_SLOW + 2:
                previous_rsi = _calc_rsi(closes[:-1], RSI_PERIOD_SLOW)
                if previous_rsi is not None:
                    previous_rsis.append(previous_rsi)

        if not current_rsis:
            return {"avg_rsi": 50.0, "avg_rsi_change": 0.0}

        avg_rsi = float(np.mean(current_rsis))
        avg_prev_rsi = float(np.mean(previous_rsis)) if previous_rsis else avg_rsi
        return {"avg_rsi": avg_rsi, "avg_rsi_change": avg_rsi - avg_prev_rsi}

    def _evaluate_signals(self, bar: prepare.BarData) -> dict[str, object] | None:
        """Evaluate all technical signals and return vote counts.

        Returns a dict with:
        - rsi_fast, rsi_slow: RSI values
        - bull_votes: count of bullish signals
        - bear_votes: count of bearish signals
        - bull_flags: dict of which bull signals triggered
        - bear_flags: dict of which bear signals triggered
        Returns None if insufficient history.
        """
        if len(bar.history) < max(RSI_PERIOD_SLOW, BB_PERIOD, EMA_SLOW, MACD_SLOW + MACD_SIGNAL) + 1:
            return None

        closes = bar.history["close"].values
        volumes = bar.history["volume"].values

        # Calculate indicators
        rsi_fast = _calc_rsi(closes, RSI_PERIOD_FAST)
        rsi_slow = _calc_rsi(closes, RSI_PERIOD_SLOW)
        macd_result = _calc_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        bb_result = _calc_bollinger(closes, BB_PERIOD, BB_STD)
        ema_fast_result = _calc_ema(closes, EMA_FAST)
        ema_slow_result = _calc_ema(closes, EMA_SLOW)
        momentum = _calc_momentum(closes, MOMENTUM_PERIOD)
        avg_volume = _calc_average_volume(volumes, VOLUME_LOOKBACK)

        if rsi_slow is None:
            return None

        current_close = closes[-1]
        current_volume = volumes[-1]

        # Bull votes
        bull_flags = {
            "dual_rsi_oversold": rsi_slow <= RSI_OVERSOLD and (rsi_fast is not None and rsi_fast <= RSI_OVERSOLD),
            "macd_histogram_positive": macd_result is not None and macd_result[2] > 0,  # histogram > 0
            "close_below_bb_lower": bb_result is not None and current_close < bb_result[2],  # close < lower
            "ema_fast_above_slow": ema_fast_result is not None and ema_slow_result is not None and ema_fast_result[-1] > ema_slow_result[-1],
            "momentum_positive": momentum is not None and momentum > 0,
            "volume_above_avg": avg_volume is not None and current_volume > avg_volume * VOLUME_THRESHOLD,
        }
        bull_votes = sum(1 for v in bull_flags.values() if v)

        # Bear votes
        bear_flags = {
            "macd_histogram_negative": macd_result is not None and macd_result[2] < 0,  # histogram < 0
            "close_above_bb_upper": bb_result is not None and current_close > bb_result[0],  # close > upper
            "ema_fast_below_slow": ema_fast_result is not None and ema_slow_result is not None and ema_fast_result[-1] < ema_slow_result[-1],
            "momentum_negative": momentum is not None and momentum < 0,
            "rsi_slow_high": rsi_slow > RSI_EXIT,  # Slow RSI above exit threshold
        }
        bear_votes = sum(1 for v in bear_flags.values() if v)

        return {
            "rsi_fast": rsi_fast,
            "rsi_slow": rsi_slow,
            "bull_votes": bull_votes,
            "bear_votes": bear_votes,
            "weighted_bull_votes": bull_votes + int(bull_flags["dual_rsi_oversold"]),
            "bull_flags": bull_flags,
            "bear_flags": bear_flags,
            "macd": macd_result,
            "bb": bb_result,
        }

    def on_bar(
        self,
        bar_data: dict[str, prepare.BarData],
        portfolio: prepare.PortfolioState,
    ) -> list[prepare.Signal]:
        signals: list[prepare.Signal] = []
        current_positions = set(portfolio.positions.keys())
        market_state = self._market_state(bar_data)

        for symbol, bar in bar_data.items():
            # Evaluate all signals and votes
            signal_data = self._evaluate_signals(bar)
            if signal_data is None:
                continue

            rsi_fast = signal_data["rsi_fast"]
            rsi_slow = signal_data["rsi_slow"]
            bull_votes = signal_data["bull_votes"]
            bear_votes = signal_data["bear_votes"]
            weighted_bull_votes = signal_data.get("weighted_bull_votes", bull_votes)
            bull_flags = signal_data["bull_flags"]
            is_held = symbol in current_positions

            # Sell logic - hard exits in priority order
            if is_held:
                avg_price = portfolio.avg_prices.get(symbol, 0)

                # 1. Stop-loss (highest priority)
                if avg_price > 0 and bar.close < avg_price * (1 - STOP_LOSS_PCT):
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=f"Stop-loss ({(bar.close/avg_price - 1)*100:.1f}%)",
                    ))
                    current_positions.discard(symbol)
                    self._stop_loss_dates[symbol] = portfolio.date
                    continue

                # 2. RSI recovery exit (when profitable)
                if rsi_slow >= RSI_EXIT:
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=f"RSI recovered to {rsi_slow:.0f}",
                    ))
                    current_positions.discard(symbol)
                    continue

                # 3. Max holding period exit
                entry_date = portfolio.position_dates.get(symbol)
                if entry_date:
                    holding_days = self._days_between(entry_date, portfolio.date)
                    if holding_days >= HOLDING_DAYS:
                        signals.append(prepare.Signal(
                            symbol=symbol, action="sell", weight=1.0,
                            reason=f"Max holding {holding_days}d",
                        ))
                        current_positions.discard(symbol)
                        continue

                # 4. Bear-vote exit (optional, only if no hard exit triggered)
                if bear_votes >= MIN_SELL_VOTES:
                    reason = _format_vote_reason(
                        "Bear", bear_votes, signal_data["bear_flags"], 3
                    )
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=reason,
                    ))
                    current_positions.discard(symbol)
                    continue

            # Buy logic - vote threshold based
            if not is_held and len(current_positions) < MAX_POSITIONS:
                # Check cooldown after stop-loss
                if symbol in self._stop_loss_dates:
                    days_since_sl = self._days_between(self._stop_loss_dates[symbol], portfolio.date)
                    if days_since_sl < COOLDOWN_DAYS:
                        continue
                    else:
                        del self._stop_loss_dates[symbol]

                special_reversion_buy = (
                    bull_votes == MIN_VOTES - 1
                    and bull_flags.get("dual_rsi_oversold", False)
                    and bull_flags.get("close_below_bb_lower", False)
                    and weighted_bull_votes >= MIN_WEIGHTED_BUY_VOTES
                )

                # Buy on sufficient bull votes
                allow_high_rsi_buy = (
                    not BLOCK_HIGH_RSI_BUYS
                    or not signal_data["bear_flags"]["rsi_slow_high"]
                    or bull_flags["dual_rsi_oversold"]
                )
                allow_falling_market_buy = (
                    not FALLING_MARKET_BLOCK_BUYS
                    or market_state["avg_rsi"] < FALLING_MARKET_RSI_LEVEL
                    or market_state["avg_rsi_change"] > FALLING_MARKET_CHANGE
                    or bull_flags["dual_rsi_oversold"]
                )
                allow_extreme_fall_buy = (
                    not bull_flags["dual_rsi_oversold"]
                    or market_state["avg_rsi_change"] >= EXTREME_FALLING_MARKET_CHANGE
                    or bull_flags["macd_histogram_positive"]
                )
                pure_reversion_buy = (
                    bull_flags["dual_rsi_oversold"]
                    and bull_flags["close_below_bb_lower"]
                    and bull_flags["volume_above_avg"]
                    and not bull_flags["macd_histogram_positive"]
                )
                pure_trend_buy = (
                    bull_flags["macd_histogram_positive"]
                    and bull_flags["ema_fast_above_slow"]
                    and bull_flags["momentum_positive"]
                    and bull_flags["volume_above_avg"]
                    and not bull_flags["dual_rsi_oversold"]
                )
                allow_reversion_regime_buy = (
                    not pure_reversion_buy
                    or market_state["avg_rsi_change"] < REVERSION_MARKET_CHANGE_CEILING
                )
                allow_trend_regime_buy = (
                    not pure_trend_buy
                    or market_state["avg_rsi"] < OVERHEATED_MARKET_RSI_LEVEL
                )
                if (
                    (bull_votes >= MIN_VOTES or special_reversion_buy)
                    and weighted_bull_votes >= MIN_WEIGHTED_BUY_VOTES
                    and allow_high_rsi_buy
                    and allow_falling_market_buy
                    and allow_extreme_fall_buy
                    and allow_reversion_regime_buy
                    and allow_trend_regime_buy
                ):
                    reason = _format_vote_reason(
                        "Bull", bull_votes, bull_flags, 4
                    )
                    signals.append(prepare.Signal(
                        symbol=symbol, action="buy", weight=POSITION_SIZE,
                        reason=reason,
                    ))
                    current_positions.add(symbol)

        return signals
