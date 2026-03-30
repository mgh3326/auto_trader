"""Backtest strategy implementation."""

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

import prepare
from indicators import (
    _calc_average_volume,
    _calc_bollinger,
    _calc_ema,
    _calc_macd,
    _calc_momentum,
    _calc_rsi,
)

if TYPE_CHECKING:
    from pandas import Series

# Unified Strategy Parameters
PARAMS = {
    # RSI Parameters
    "rsi_period_fast": 6,
    "rsi_period_slow": 14,
    "rsi_oversold": 30,
    "rsi_exit": 55,
    # Position Sizing
    "max_positions": 5,
    "position_size": 0.10,
    "strong_reversion_position_size": 0.15,
    "btc_hot_stall_trend_position_size": 0.0025,
    "btc_mid_hot_accel_trend_position_size": 0.00,
    "ada_stalled_washout_reversion_position_size": 0.00,
    "dot_mild_reversion_position_size": 0.00015625,
    "sol_hot_stall_trend_position_size": 0.00,
    "link_hot_stall_trend_position_size": 0.00,
    "xrp_stalled_washout_reversion_position_size": 0.00,
    "sol_mild_reversion_position_size": 0.00,
    "sol_low_breadth_trend_position_size": 0.00,
    "avax_trend_position_size": 0.04,
    "xrp_trend_position_size": 0.00,
    "eth_pure_reversion_position_size": 0.00,
    # Exit Parameters
    "holding_days": 21,
    "stop_loss_pct": 0.02,
    "cooldown_days": 15,
    # Voting Parameters
    "min_votes": 4,
    "min_weighted_buy_votes": 4,
    "min_sell_votes": 2,
    "block_high_rsi_buys": False,
    "total_bull_signals": 6,
    # Market State Filters
    "falling_market_block_buys": True,
    "falling_market_rsi_level": 55.0,
    "falling_market_change": -1.0,
    "extreme_falling_market_change": -6.0,
    "reversion_market_change_ceiling": -2.0,
    "overheated_market_rsi_level": 75.0,
    # Symbol-Specific Thresholds
    "avax_strong_reversion_max_market_rsi": 35.0,
    "avax_trend_min_market_rsi": 60.0,
    "btc_trend_hot_rsi_level": 70.0,
    "btc_trend_stall_change": 2.0,
    "btc_mid_hot_rsi_low": 60.0,
    "btc_mid_hot_rsi_high": 65.0,
    "btc_extreme_accel_change": 15.0,
    "ada_stalled_washout_rsi": 26.0,
    "ada_stalled_washout_change": -2.5,
    "xrp_stalled_washout_rsi": 26.0,
    "xrp_stalled_washout_change": -2.5,
    "sol_mild_reversion_rsi": 32.0,
    "dot_mild_reversion_rsi": 33.0,
    "sol_hot_stall_rsi_low": 66.0,
    "sol_hot_stall_rsi_high": 68.0,
    "sol_hot_stall_change": 1.5,
    "link_hot_stall_rsi_low": 66.0,
    "link_hot_stall_rsi_high": 68.0,
    "link_hot_stall_change": 1.5,
    "sol_low_breadth_rsi": 49.0,
    "sol_low_breadth_change": -5.0,
    "link_trend_rsi_low": 58.0,
    "link_trend_rsi_high": 60.0,
    "link_trend_max_acceleration": 6.0,
    "dot_trend_mid_rsi_min_change": 12.0,
    "trend_mid_rsi_low": 60.0,
    "trend_mid_rsi_high": 65.0,
    "trend_mid_rsi_min_change": 5.0,
    "trend_hot_rsi_level": 64.0,
    "trend_trap_change_low": 3.0,
    "trend_trap_change_high": 9.0,
    # Indicator Periods
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bb_period": 15,
    "bb_std": 1.5,
    "ema_fast": 8,
    "ema_slow": 24,
    "momentum_period": 5,
    "volume_lookback": 20,
    "volume_threshold": 1.5,
}

# Minimum history bars required for all indicators
MIN_HISTORY_BARS = (
    max(
        PARAMS["rsi_period_slow"],
        PARAMS["bb_period"],
        PARAMS["ema_slow"],
        PARAMS["macd_slow"] + PARAMS["macd_signal"],
    )
    + 1
)

# Backward compatibility aliases (to be removed after full migration)
RSI_PERIOD_FAST = PARAMS["rsi_period_fast"]
RSI_PERIOD_SLOW = PARAMS["rsi_period_slow"]
RSI_OVERSOLD = PARAMS["rsi_oversold"]
RSI_EXIT = PARAMS["rsi_exit"]
MAX_POSITIONS = PARAMS["max_positions"]
POSITION_SIZE = PARAMS["position_size"]
STRONG_REVERSION_POSITION_SIZE = PARAMS["strong_reversion_position_size"]
BTC_HOT_STALL_TREND_POSITION_SIZE = PARAMS["btc_hot_stall_trend_position_size"]
BTC_MID_HOT_ACCEL_TREND_POSITION_SIZE = PARAMS["btc_mid_hot_accel_trend_position_size"]
ADA_STALLED_WASHOUT_REVERSION_POSITION_SIZE = PARAMS[
    "ada_stalled_washout_reversion_position_size"
]
DOT_MILD_REVERSION_POSITION_SIZE = PARAMS["dot_mild_reversion_position_size"]
SOL_HOT_STALL_TREND_POSITION_SIZE = PARAMS["sol_hot_stall_trend_position_size"]
LINK_HOT_STALL_TREND_POSITION_SIZE = PARAMS["link_hot_stall_trend_position_size"]
XRP_STALLED_WASHOUT_REVERSION_POSITION_SIZE = PARAMS[
    "xrp_stalled_washout_reversion_position_size"
]
SOL_MILD_REVERSION_POSITION_SIZE = PARAMS["sol_mild_reversion_position_size"]
SOL_LOW_BREADTH_TREND_POSITION_SIZE = PARAMS["sol_low_breadth_trend_position_size"]
AVAX_TREND_POSITION_SIZE = PARAMS["avax_trend_position_size"]
XRP_TREND_POSITION_SIZE = PARAMS["xrp_trend_position_size"]
ETH_PURE_REVERSION_POSITION_SIZE = PARAMS["eth_pure_reversion_position_size"]
HOLDING_DAYS = PARAMS["holding_days"]
STOP_LOSS_PCT = PARAMS["stop_loss_pct"]
COOLDOWN_DAYS = PARAMS["cooldown_days"]
MIN_VOTES = PARAMS["min_votes"]
MIN_WEIGHTED_BUY_VOTES = PARAMS["min_weighted_buy_votes"]
MIN_SELL_VOTES = PARAMS["min_sell_votes"]
BLOCK_HIGH_RSI_BUYS = PARAMS["block_high_rsi_buys"]
TOTAL_BULL_SIGNALS = PARAMS["total_bull_signals"]
FALLING_MARKET_BLOCK_BUYS = PARAMS["falling_market_block_buys"]
FALLING_MARKET_RSI_LEVEL = PARAMS["falling_market_rsi_level"]
FALLING_MARKET_CHANGE = PARAMS["falling_market_change"]
EXTREME_FALLING_MARKET_CHANGE = PARAMS["extreme_falling_market_change"]
REVERSION_MARKET_CHANGE_CEILING = PARAMS["reversion_market_change_ceiling"]
AVAX_STRONG_REVERSION_MAX_MARKET_RSI = PARAMS["avax_strong_reversion_max_market_rsi"]
AVAX_TREND_MIN_MARKET_RSI = PARAMS["avax_trend_min_market_rsi"]
BTC_TREND_HOT_RSI_LEVEL = PARAMS["btc_trend_hot_rsi_level"]
BTC_TREND_STALL_CHANGE = PARAMS["btc_trend_stall_change"]
BTC_MID_HOT_RSI_LOW = PARAMS["btc_mid_hot_rsi_low"]
BTC_MID_HOT_RSI_HIGH = PARAMS["btc_mid_hot_rsi_high"]
BTC_EXTREME_ACCEL_CHANGE = PARAMS["btc_extreme_accel_change"]
ADA_STALLED_WASHOUT_RSI = PARAMS["ada_stalled_washout_rsi"]
ADA_STALLED_WASHOUT_CHANGE = PARAMS["ada_stalled_washout_change"]
XRP_STALLED_WASHOUT_RSI = PARAMS["xrp_stalled_washout_rsi"]
XRP_STALLED_WASHOUT_CHANGE = PARAMS["xrp_stalled_washout_change"]
SOL_MILD_REVERSION_RSI = PARAMS["sol_mild_reversion_rsi"]
DOT_MILD_REVERSION_RSI = PARAMS["dot_mild_reversion_rsi"]
SOL_HOT_STALL_RSI_LOW = PARAMS["sol_hot_stall_rsi_low"]
SOL_HOT_STALL_RSI_HIGH = PARAMS["sol_hot_stall_rsi_high"]
SOL_HOT_STALL_CHANGE = PARAMS["sol_hot_stall_change"]
LINK_HOT_STALL_RSI_LOW = PARAMS["link_hot_stall_rsi_low"]
LINK_HOT_STALL_RSI_HIGH = PARAMS["link_hot_stall_rsi_high"]
LINK_HOT_STALL_CHANGE = PARAMS["link_hot_stall_change"]
SOL_LOW_BREADTH_RSI = PARAMS["sol_low_breadth_rsi"]
SOL_LOW_BREADTH_CHANGE = PARAMS["sol_low_breadth_change"]
LINK_TREND_RSI_LOW = PARAMS["link_trend_rsi_low"]
LINK_TREND_RSI_HIGH = PARAMS["link_trend_rsi_high"]
LINK_TREND_MAX_ACCELERATION = PARAMS["link_trend_max_acceleration"]
DOT_TREND_MID_RSI_MIN_CHANGE = PARAMS["dot_trend_mid_rsi_min_change"]
OVERHEATED_MARKET_RSI_LEVEL = PARAMS["overheated_market_rsi_level"]
TREND_MID_RSI_LOW = PARAMS["trend_mid_rsi_low"]
TREND_MID_RSI_HIGH = PARAMS["trend_mid_rsi_high"]
TREND_MID_RSI_MIN_CHANGE = PARAMS["trend_mid_rsi_min_change"]
TREND_HOT_RSI_LEVEL = PARAMS["trend_hot_rsi_level"]
TREND_TRAP_CHANGE_LOW = PARAMS["trend_trap_change_low"]
TREND_TRAP_CHANGE_HIGH = PARAMS["trend_trap_change_high"]
MACD_FAST = PARAMS["macd_fast"]
MACD_SLOW = PARAMS["macd_slow"]
MACD_SIGNAL = PARAMS["macd_signal"]
BB_PERIOD = PARAMS["bb_period"]
BB_STD = PARAMS["bb_std"]
EMA_FAST = PARAMS["ema_fast"]
EMA_SLOW = PARAMS["ema_slow"]
MOMENTUM_PERIOD = PARAMS["momentum_period"]
VOLUME_LOOKBACK = PARAMS["volume_lookback"]
VOLUME_THRESHOLD = PARAMS["volume_threshold"]


def _format_vote_reason(
    prefix: str, votes: int, flags: dict[str, bool], limit: int
) -> str:
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
        if (
            len(bar.history)
            < max(RSI_PERIOD_SLOW, BB_PERIOD, EMA_SLOW, MACD_SLOW + MACD_SIGNAL) + 1
        ):
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
            "dual_rsi_oversold": rsi_slow <= RSI_OVERSOLD
            and (rsi_fast is not None and rsi_fast <= RSI_OVERSOLD),
            "macd_histogram_positive": macd_result is not None
            and macd_result[2] > 0,  # histogram > 0
            "close_below_bb_lower": bb_result is not None
            and current_close < bb_result[2],  # close < lower
            "ema_fast_above_slow": ema_fast_result is not None
            and ema_slow_result is not None
            and ema_fast_result[-1] > ema_slow_result[-1],
            "momentum_positive": momentum is not None and momentum > 0,
            "volume_above_avg": avg_volume is not None
            and current_volume > avg_volume * VOLUME_THRESHOLD,
        }
        bull_votes = sum(1 for v in bull_flags.values() if v)

        # Bear votes
        bear_flags = {
            "macd_histogram_negative": macd_result is not None
            and macd_result[2] < 0,  # histogram < 0
            "close_above_bb_upper": bb_result is not None
            and current_close > bb_result[0],  # close > upper
            "ema_fast_below_slow": ema_fast_result is not None
            and ema_slow_result is not None
            and ema_fast_result[-1] < ema_slow_result[-1],
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
            bear_flags = signal_data["bear_flags"]
            dual_rsi_oversold = bull_flags.get("dual_rsi_oversold", False)
            macd_histogram_positive = bull_flags.get("macd_histogram_positive", False)
            close_below_bb_lower = bull_flags.get("close_below_bb_lower", False)
            ema_fast_above_slow = bull_flags.get("ema_fast_above_slow", False)
            momentum_positive = bull_flags.get("momentum_positive", False)
            volume_above_avg = bull_flags.get("volume_above_avg", False)
            is_held = symbol in current_positions

            # Sell logic - hard exits in priority order
            if is_held:
                avg_price = portfolio.avg_prices.get(symbol, 0)

                # 1. Stop-loss (highest priority)
                if avg_price > 0 and bar.close < avg_price * (1 - STOP_LOSS_PCT):
                    signals.append(
                        prepare.Signal(
                            symbol=symbol,
                            action="sell",
                            weight=1.0,
                            reason=f"Stop-loss ({(bar.close / avg_price - 1) * 100:.1f}%)",
                        )
                    )
                    current_positions.discard(symbol)
                    self._stop_loss_dates[symbol] = portfolio.date
                    continue

                # 2. RSI recovery exit (when profitable)
                if rsi_slow >= RSI_EXIT:
                    signals.append(
                        prepare.Signal(
                            symbol=symbol,
                            action="sell",
                            weight=1.0,
                            reason=f"RSI recovered to {rsi_slow:.0f}",
                        )
                    )
                    current_positions.discard(symbol)
                    continue

                # 3. Max holding period exit
                entry_date = portfolio.position_dates.get(symbol)
                if entry_date:
                    holding_days = self._days_between(entry_date, portfolio.date)
                    if holding_days >= HOLDING_DAYS:
                        signals.append(
                            prepare.Signal(
                                symbol=symbol,
                                action="sell",
                                weight=1.0,
                                reason=f"Max holding {holding_days}d",
                            )
                        )
                        current_positions.discard(symbol)
                        continue

                # 4. Bear-vote exit (optional, only if no hard exit triggered)
                if bear_votes >= MIN_SELL_VOTES:
                    reason = _format_vote_reason(
                        "Bear", bear_votes, signal_data["bear_flags"], 3
                    )
                    signals.append(
                        prepare.Signal(
                            symbol=symbol,
                            action="sell",
                            weight=1.0,
                            reason=reason,
                        )
                    )
                    current_positions.discard(symbol)
                    continue

            # Buy logic - vote threshold based
            if not is_held and len(current_positions) < MAX_POSITIONS:
                # Check cooldown after stop-loss
                if symbol in self._stop_loss_dates:
                    days_since_sl = self._days_between(
                        self._stop_loss_dates[symbol], portfolio.date
                    )
                    if days_since_sl < COOLDOWN_DAYS:
                        continue
                    else:
                        del self._stop_loss_dates[symbol]

                special_reversion_buy = (
                    bull_votes == MIN_VOTES - 1
                    and dual_rsi_oversold
                    and close_below_bb_lower
                    and weighted_bull_votes >= MIN_WEIGHTED_BUY_VOTES
                )

                # Buy on sufficient bull votes
                allow_high_rsi_buy = (
                    not BLOCK_HIGH_RSI_BUYS
                    or not bear_flags.get("rsi_slow_high", False)
                    or dual_rsi_oversold
                )
                allow_falling_market_buy = (
                    not FALLING_MARKET_BLOCK_BUYS
                    or market_state["avg_rsi"] < FALLING_MARKET_RSI_LEVEL
                    or market_state["avg_rsi_change"] > FALLING_MARKET_CHANGE
                    or dual_rsi_oversold
                )
                allow_extreme_fall_buy = (
                    not dual_rsi_oversold
                    or market_state["avg_rsi_change"] >= EXTREME_FALLING_MARKET_CHANGE
                    or macd_histogram_positive
                )
                pure_reversion_buy = (
                    dual_rsi_oversold
                    and close_below_bb_lower
                    and volume_above_avg
                    and not macd_histogram_positive
                )
                ada_stalled_washout_buy = (
                    pure_reversion_buy
                    and symbol == "ADA"
                    and market_state["avg_rsi"] < ADA_STALLED_WASHOUT_RSI
                    and market_state["avg_rsi_change"] > ADA_STALLED_WASHOUT_CHANGE
                )
                xrp_stalled_washout_buy = (
                    pure_reversion_buy
                    and symbol == "XRP"
                    and market_state["avg_rsi"] < XRP_STALLED_WASHOUT_RSI
                    and market_state["avg_rsi_change"] > XRP_STALLED_WASHOUT_CHANGE
                )
                sol_mild_reversion_buy = (
                    pure_reversion_buy
                    and symbol == "SOL"
                    and market_state["avg_rsi"] > SOL_MILD_REVERSION_RSI
                )
                dot_mild_reversion_buy = (
                    pure_reversion_buy
                    and symbol == "DOT"
                    and market_state["avg_rsi"] > DOT_MILD_REVERSION_RSI
                )
                strong_reversion_buy = (
                    dual_rsi_oversold
                    and close_below_bb_lower
                    and macd_histogram_positive
                )
                allow_btc_pure_reversion_buy = not pure_reversion_buy or symbol != "BTC"
                allow_eth_strong_reversion_buy = (
                    not strong_reversion_buy or symbol != "ETH"
                )
                allow_avax_strong_reversion_buy = (
                    symbol != "AVAX"
                    or not strong_reversion_buy
                    or market_state["avg_rsi"] < AVAX_STRONG_REVERSION_MAX_MARKET_RSI
                )
                eth_pure_reversion_buy = pure_reversion_buy and symbol == "ETH"
                pure_trend_buy = (
                    macd_histogram_positive
                    and ema_fast_above_slow
                    and momentum_positive
                    and volume_above_avg
                    and not dual_rsi_oversold
                )
                btc_hot_stall_trend_buy = (
                    pure_trend_buy
                    and symbol == "BTC"
                    and market_state["avg_rsi"] >= BTC_TREND_HOT_RSI_LEVEL
                    and market_state["avg_rsi_change"] < BTC_TREND_STALL_CHANGE
                )
                btc_mid_hot_accel_buy = (
                    pure_trend_buy
                    and symbol == "BTC"
                    and BTC_MID_HOT_RSI_LOW
                    <= market_state["avg_rsi"]
                    < BTC_MID_HOT_RSI_HIGH
                    and market_state["avg_rsi_change"] >= BTC_EXTREME_ACCEL_CHANGE
                )
                sol_low_breadth_trend_buy = (
                    pure_trend_buy
                    and symbol == "SOL"
                    and market_state["avg_rsi"] < SOL_LOW_BREADTH_RSI
                    and market_state["avg_rsi_change"] <= SOL_LOW_BREADTH_CHANGE
                )
                sol_hot_stall_trend_buy = (
                    pure_trend_buy
                    and symbol == "SOL"
                    and SOL_HOT_STALL_RSI_LOW
                    <= market_state["avg_rsi"]
                    < SOL_HOT_STALL_RSI_HIGH
                    and market_state["avg_rsi_change"] < SOL_HOT_STALL_CHANGE
                )
                link_hot_stall_trend_buy = (
                    pure_trend_buy
                    and symbol == "LINK"
                    and LINK_HOT_STALL_RSI_LOW
                    <= market_state["avg_rsi"]
                    < LINK_HOT_STALL_RSI_HIGH
                    and market_state["avg_rsi_change"] < LINK_HOT_STALL_CHANGE
                )
                avax_pure_trend_buy = pure_trend_buy and symbol == "AVAX"
                dot_pure_trend_buy = pure_trend_buy and symbol == "DOT"
                link_pure_trend_buy = pure_trend_buy and symbol == "LINK"
                xrp_pure_trend_buy = pure_trend_buy and symbol == "XRP"
                allow_link_trend_buy = (
                    not link_pure_trend_buy
                    or market_state["avg_rsi"] < LINK_TREND_RSI_LOW
                    or market_state["avg_rsi"] >= LINK_TREND_RSI_HIGH
                    or market_state["avg_rsi_change"] <= LINK_TREND_MAX_ACCELERATION
                )
                allow_avax_trend_buy = (
                    not avax_pure_trend_buy
                    or market_state["avg_rsi"] >= AVAX_TREND_MIN_MARKET_RSI
                )
                allow_reversion_regime_buy = (
                    not pure_reversion_buy
                    or market_state["avg_rsi_change"] < REVERSION_MARKET_CHANGE_CEILING
                )
                allow_trend_regime_buy = (
                    not pure_trend_buy
                    or market_state["avg_rsi"] < OVERHEATED_MARKET_RSI_LEVEL
                )
                allow_trend_trap_buy = (
                    not pure_trend_buy
                    or market_state["avg_rsi"] < TREND_HOT_RSI_LEVEL
                    or market_state["avg_rsi_change"] <= TREND_TRAP_CHANGE_LOW
                    or market_state["avg_rsi_change"] > TREND_TRAP_CHANGE_HIGH
                )
                require_trend_acceleration = (
                    pure_trend_buy
                    and TREND_MID_RSI_LOW
                    <= market_state["avg_rsi"]
                    < TREND_MID_RSI_HIGH
                )
                allow_trend_acceleration_buy = (
                    not require_trend_acceleration
                    or market_state["avg_rsi_change"] > TREND_MID_RSI_MIN_CHANGE
                )
                allow_dot_trend_acceleration_buy = (
                    not dot_pure_trend_buy
                    or market_state["avg_rsi"] < TREND_MID_RSI_LOW
                    or market_state["avg_rsi"] >= TREND_MID_RSI_HIGH
                    or market_state["avg_rsi_change"] > DOT_TREND_MID_RSI_MIN_CHANGE
                )
                if (
                    (bull_votes >= MIN_VOTES or special_reversion_buy)
                    and weighted_bull_votes >= MIN_WEIGHTED_BUY_VOTES
                    and allow_high_rsi_buy
                    and allow_falling_market_buy
                    and allow_extreme_fall_buy
                    and allow_btc_pure_reversion_buy
                    and allow_eth_strong_reversion_buy
                    and allow_avax_strong_reversion_buy
                    and allow_link_trend_buy
                    and allow_avax_trend_buy
                    and allow_reversion_regime_buy
                    and allow_trend_regime_buy
                    and allow_trend_trap_buy
                    and allow_trend_acceleration_buy
                    and allow_dot_trend_acceleration_buy
                ):
                    reason = _format_vote_reason("Bull", bull_votes, bull_flags, 4)
                    if strong_reversion_buy:
                        buy_weight = STRONG_REVERSION_POSITION_SIZE
                    elif btc_hot_stall_trend_buy:
                        buy_weight = BTC_HOT_STALL_TREND_POSITION_SIZE
                    elif btc_mid_hot_accel_buy:
                        buy_weight = BTC_MID_HOT_ACCEL_TREND_POSITION_SIZE
                    elif sol_hot_stall_trend_buy:
                        buy_weight = SOL_HOT_STALL_TREND_POSITION_SIZE
                    elif link_hot_stall_trend_buy:
                        buy_weight = LINK_HOT_STALL_TREND_POSITION_SIZE
                    elif xrp_stalled_washout_buy:
                        buy_weight = XRP_STALLED_WASHOUT_REVERSION_POSITION_SIZE
                    elif sol_mild_reversion_buy:
                        buy_weight = SOL_MILD_REVERSION_POSITION_SIZE
                    elif sol_low_breadth_trend_buy:
                        buy_weight = SOL_LOW_BREADTH_TREND_POSITION_SIZE
                    elif ada_stalled_washout_buy:
                        buy_weight = ADA_STALLED_WASHOUT_REVERSION_POSITION_SIZE
                    elif dot_mild_reversion_buy:
                        buy_weight = DOT_MILD_REVERSION_POSITION_SIZE
                    elif eth_pure_reversion_buy:
                        buy_weight = ETH_PURE_REVERSION_POSITION_SIZE
                    elif avax_pure_trend_buy:
                        buy_weight = AVAX_TREND_POSITION_SIZE
                    elif xrp_pure_trend_buy:
                        buy_weight = XRP_TREND_POSITION_SIZE
                    else:
                        buy_weight = POSITION_SIZE
                    signals.append(
                        prepare.Signal(
                            symbol=symbol,
                            action="buy",
                            weight=buy_weight,
                            reason=reason,
                        )
                    )
                    current_positions.add(symbol)

        return signals
