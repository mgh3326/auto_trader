"""Backtest strategy implementation."""

from dataclasses import dataclass

import numpy as np
import prepare
from indicators import (
    _calc_average_volume,
    _calc_bollinger,
    _calc_ema,
    _calc_macd,
    _calc_momentum,
    _calc_rsi,
)


@dataclass(frozen=True)
class SignalContext:
    closes: np.ndarray
    volumes: np.ndarray
    current_close: float
    current_volume: float
    rsi_fast: float | None
    rsi_slow: float | None
    macd: tuple[float, float, float] | None
    bb: tuple[float, float, float] | None
    ema_fast: np.ndarray | None
    ema_slow: np.ndarray | None
    momentum: float | None
    avg_volume: float | None


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


# Bull signal functions
def _signal_dual_rsi_oversold(ctx: SignalContext, params: dict) -> bool:
    return (
        ctx.rsi_slow is not None
        and ctx.rsi_slow <= params["rsi_oversold"]
        and ctx.rsi_fast is not None
        and ctx.rsi_fast <= params["rsi_oversold"]
    )


def _signal_macd_histogram_positive(ctx: SignalContext, params: dict) -> bool:
    return ctx.macd is not None and ctx.macd[2] > 0


def _signal_close_below_bb_lower(ctx: SignalContext, params: dict) -> bool:
    return ctx.bb is not None and ctx.current_close < ctx.bb[2]


def _signal_ema_fast_above_slow(ctx: SignalContext, params: dict) -> bool:
    return (
        ctx.ema_fast is not None
        and ctx.ema_slow is not None
        and ctx.ema_fast[-1] > ctx.ema_slow[-1]
    )


def _signal_momentum_positive(ctx: SignalContext, params: dict) -> bool:
    return ctx.momentum is not None and ctx.momentum > 0


def _signal_volume_above_avg(ctx: SignalContext, params: dict) -> bool:
    return (
        ctx.avg_volume is not None
        and ctx.current_volume > ctx.avg_volume * params["volume_threshold"]
    )


# Bear signal functions
def _signal_macd_histogram_negative(ctx: SignalContext, params: dict) -> bool:
    return ctx.macd is not None and ctx.macd[2] < 0


def _signal_close_above_bb_upper(ctx: SignalContext, params: dict) -> bool:
    return ctx.bb is not None and ctx.current_close > ctx.bb[0]


def _signal_ema_fast_below_slow(ctx: SignalContext, params: dict) -> bool:
    return (
        ctx.ema_fast is not None
        and ctx.ema_slow is not None
        and ctx.ema_fast[-1] < ctx.ema_slow[-1]
    )


def _signal_momentum_negative(ctx: SignalContext, params: dict) -> bool:
    return ctx.momentum is not None and ctx.momentum < 0


def _signal_rsi_slow_high(ctx: SignalContext, params: dict) -> bool:
    return ctx.rsi_slow is not None and ctx.rsi_slow > params["rsi_exit"]


def _setup_special_reversion_buy(
    bull_flags: dict, bull_votes: int, weighted_bull_votes: int, params: dict
) -> bool:
    return (
        bull_votes == params["min_votes"] - 1
        and bull_flags.get("dual_rsi_oversold", False)
        and bull_flags.get("close_below_bb_lower", False)
        and weighted_bull_votes >= params["min_weighted_buy_votes"]
    )


def _setup_pure_reversion_buy(bull_flags: dict, params: dict) -> bool:
    return (
        bull_flags.get("dual_rsi_oversold", False)
        and bull_flags.get("close_below_bb_lower", False)
        and bull_flags.get("volume_above_avg", False)
        and not bull_flags.get("macd_histogram_positive", False)
    )


def _setup_strong_reversion_buy(bull_flags: dict, params: dict) -> bool:
    return (
        bull_flags.get("dual_rsi_oversold", False)
        and bull_flags.get("close_below_bb_lower", False)
        and bull_flags.get("macd_histogram_positive", False)
    )


def _setup_pure_trend_buy(bull_flags: dict, params: dict) -> bool:
    return (
        bull_flags.get("macd_histogram_positive", False)
        and bull_flags.get("ema_fast_above_slow", False)
        and bull_flags.get("momentum_positive", False)
        and bull_flags.get("volume_above_avg", False)
        and not bull_flags.get("dual_rsi_oversold", False)
    )


# Symbol-specific buy rules: (symbol, setup_fn, market_fn, size_key)
SYMBOL_BUY_RULES = [
    (
        "*",
        lambda s, b, p: _setup_strong_reversion_buy(b, p),
        None,
        "strong_reversion_position_size",
    ),
    (
        "BTC",
        lambda s, b, p: _setup_pure_trend_buy(b, p),
        lambda m, p: (
            m["avg_rsi"] >= p["btc_trend_hot_rsi_level"]
            and m["avg_rsi_change"] < p["btc_trend_stall_change"]
        ),
        "btc_hot_stall_trend_position_size",
    ),
    (
        "BTC",
        lambda s, b, p: _setup_pure_trend_buy(b, p),
        lambda m, p: (
            p["btc_mid_hot_rsi_low"] <= m["avg_rsi"] < p["btc_mid_hot_rsi_high"]
            and m["avg_rsi_change"] >= p["btc_extreme_accel_change"]
        ),
        "btc_mid_hot_accel_trend_position_size",
    ),
    (
        "SOL",
        lambda s, b, p: _setup_pure_trend_buy(b, p),
        lambda m, p: (
            p["sol_hot_stall_rsi_low"] <= m["avg_rsi"] < p["sol_hot_stall_rsi_high"]
            and m["avg_rsi_change"] < p["sol_hot_stall_change"]
        ),
        "sol_hot_stall_trend_position_size",
    ),
    (
        "LINK",
        lambda s, b, p: _setup_pure_trend_buy(b, p),
        lambda m, p: (
            p["link_hot_stall_rsi_low"] <= m["avg_rsi"] < p["link_hot_stall_rsi_high"]
            and m["avg_rsi_change"] < p["link_hot_stall_change"]
        ),
        "link_hot_stall_trend_position_size",
    ),
    (
        "XRP",
        lambda s, b, p: _setup_pure_reversion_buy(b, p) and s == "XRP",
        lambda m, p: (
            m["avg_rsi"] < p["xrp_stalled_washout_rsi"]
            and m["avg_rsi_change"] > p["xrp_stalled_washout_change"]
        ),
        "xrp_stalled_washout_reversion_position_size",
    ),
    (
        "SOL",
        lambda s, b, p: _setup_pure_reversion_buy(b, p) and s == "SOL",
        lambda m, p: m["avg_rsi"] > p["sol_mild_reversion_rsi"],
        "sol_mild_reversion_position_size",
    ),
    (
        "SOL",
        lambda s, b, p: _setup_pure_trend_buy(b, p) and s == "SOL",
        lambda m, p: (
            m["avg_rsi"] < p["sol_low_breadth_rsi"]
            and m["avg_rsi_change"] <= p["sol_low_breadth_change"]
        ),
        "sol_low_breadth_trend_position_size",
    ),
    (
        "ADA",
        lambda s, b, p: _setup_pure_reversion_buy(b, p) and s == "ADA",
        lambda m, p: (
            m["avg_rsi"] < p["ada_stalled_washout_rsi"]
            and m["avg_rsi_change"] > p["ada_stalled_washout_change"]
        ),
        "ada_stalled_washout_reversion_position_size",
    ),
    (
        "DOT",
        lambda s, b, p: _setup_pure_reversion_buy(b, p) and s == "DOT",
        lambda m, p: m["avg_rsi"] > p["dot_mild_reversion_rsi"],
        "dot_mild_reversion_position_size",
    ),
    (
        "ETH",
        lambda s, b, p: _setup_pure_reversion_buy(b, p) and s == "ETH",
        None,
        "eth_pure_reversion_position_size",
    ),
    (
        "AVAX",
        lambda s, b, p: _setup_pure_trend_buy(b, p) and s == "AVAX",
        None,
        "avax_trend_position_size",
    ),
    (
        "XRP",
        lambda s, b, p: _setup_pure_trend_buy(b, p) and s == "XRP",
        None,
        "xrp_trend_position_size",
    ),
]


def _resolve_symbol_buy_weight(
    symbol: str,
    bull_flags: dict,
    market_state: dict,
    params: dict,
) -> tuple[float, str | None]:
    for rule_symbol, setup_fn, market_fn, size_key in SYMBOL_BUY_RULES:
        if rule_symbol != "*" and rule_symbol != symbol:
            continue
        if not setup_fn(symbol, bull_flags, params):
            continue
        if market_fn is not None and not market_fn(market_state, params):
            continue
        return params[size_key], size_key
    return params["position_size"], None


# Signal registries - order matters for reason string formatting
BULL_SIGNALS = [
    ("dual_rsi_oversold", _signal_dual_rsi_oversold),
    ("macd_histogram_positive", _signal_macd_histogram_positive),
    ("close_below_bb_lower", _signal_close_below_bb_lower),
    ("ema_fast_above_slow", _signal_ema_fast_above_slow),
    ("momentum_positive", _signal_momentum_positive),
    ("volume_above_avg", _signal_volume_above_avg),
]

BEAR_SIGNALS = [
    ("macd_histogram_negative", _signal_macd_histogram_negative),
    ("close_above_bb_upper", _signal_close_above_bb_upper),
    ("ema_fast_below_slow", _signal_ema_fast_below_slow),
    ("momentum_negative", _signal_momentum_negative),
    ("rsi_slow_high", _signal_rsi_slow_high),
]


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
        if len(bar.history) < MIN_HISTORY_BARS:
            return None

        closes = bar.history["close"].values
        volumes = bar.history["volume"].values

        # Calculate indicators
        rsi_fast = _calc_rsi(closes, PARAMS["rsi_period_fast"])
        rsi_slow = _calc_rsi(closes, PARAMS["rsi_period_slow"])
        macd_result = _calc_macd(
            closes,
            PARAMS["macd_fast"],
            PARAMS["macd_slow"],
            PARAMS["macd_signal"],
        )
        bb_result = _calc_bollinger(closes, PARAMS["bb_period"], PARAMS["bb_std"])
        ema_fast_result = _calc_ema(closes, PARAMS["ema_fast"])
        ema_slow_result = _calc_ema(closes, PARAMS["ema_slow"])
        momentum = _calc_momentum(closes, PARAMS["momentum_period"])
        avg_volume = _calc_average_volume(volumes, PARAMS["volume_lookback"])

        if rsi_slow is None:
            return None

        ctx = SignalContext(
            closes=closes,
            volumes=volumes,
            current_close=closes[-1],
            current_volume=volumes[-1],
            rsi_fast=rsi_fast,
            rsi_slow=rsi_slow,
            macd=macd_result,
            bb=bb_result,
            ema_fast=ema_fast_result,
            ema_slow=ema_slow_result,
            momentum=momentum,
            avg_volume=avg_volume,
        )

        bull_flags = {}
        for name, fn in BULL_SIGNALS:
            bull_flags[name] = fn(ctx, PARAMS)
        bull_votes = sum(1 for v in bull_flags.values() if v)

        bear_flags = {}
        for name, fn in BEAR_SIGNALS:
            bear_flags[name] = fn(ctx, PARAMS)
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

            rsi_slow = signal_data["rsi_slow"]
            bull_votes = signal_data["bull_votes"]
            bear_votes = signal_data["bear_votes"]
            weighted_bull_votes = signal_data.get("weighted_bull_votes", bull_votes)
            bull_flags = signal_data["bull_flags"]
            bear_flags = signal_data["bear_flags"]
            dual_rsi_oversold = bull_flags.get("dual_rsi_oversold", False)
            macd_histogram_positive = bull_flags.get("macd_histogram_positive", False)
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

                special_reversion_buy = _setup_special_reversion_buy(
                    bull_flags,
                    bull_votes,
                    weighted_bull_votes,
                    PARAMS,
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
                pure_reversion_buy = _setup_pure_reversion_buy(bull_flags, PARAMS)
                strong_reversion_buy = _setup_strong_reversion_buy(bull_flags, PARAMS)
                allow_btc_pure_reversion_buy = not pure_reversion_buy or symbol != "BTC"
                allow_eth_strong_reversion_buy = (
                    not strong_reversion_buy or symbol != "ETH"
                )
                allow_avax_strong_reversion_buy = (
                    symbol != "AVAX"
                    or not strong_reversion_buy
                    or market_state["avg_rsi"] < AVAX_STRONG_REVERSION_MAX_MARKET_RSI
                )
                pure_trend_buy = _setup_pure_trend_buy(bull_flags, PARAMS)
                avax_pure_trend_buy = pure_trend_buy and symbol == "AVAX"
                dot_pure_trend_buy = pure_trend_buy and symbol == "DOT"
                link_pure_trend_buy = pure_trend_buy and symbol == "LINK"
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
                    buy_weight, _ = _resolve_symbol_buy_weight(
                        symbol,
                        bull_flags,
                        market_state,
                        PARAMS,
                    )
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
