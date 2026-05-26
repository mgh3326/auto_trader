"""ROB-316 — deterministic ICT-like entry signal (pure, testable).

fee_sweep proved the tight micro-breakout has no gross edge: the problem is
entry SELECTIVITY, not cost. This module layers deterministic ICT-flavored
filters on top of the trend breakout so that fewer, higher-quality entries fire.
Every rule is a pure function of a closed-candle sequence — no chart reading, no
lookahead, no network.

Filters (each toggleable so we can ablate):
* session / killzone   — only trade in configured UTC hours (London/NY)
* volatility (ATR)      — require ATR(n) >= floor in bps (skip dead chop)
* breakout confirmation — sma_fast>sma_slow AND close > prior breakout high
* FVG (fair value gap)  — a bullish 3-bar imbalance present in recent bars
* liquidity sweep       — prior swing low taken out then reclaimed (optional)

Long-only (spot) for the MVP; the short mirror is a later slice. Reuses the
production ``Candle``/``SignalDecision`` shapes so the Nautilus bridge and the
backtest/fee-sweep harness work unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.signal import Candle, SignalDecision

_BPS = Decimal("10000")
_MS_PER_HOUR = 3_600_000

# ICT killzones in UTC (approx): London 07-10, New York 12-15.
_DEFAULT_KILLZONE_HOURS = frozenset({7, 8, 9, 12, 13, 14})


@dataclass(frozen=True)
class IctConfig:
    # breakout base
    sma_fast: int = 7
    sma_slow: int = 25
    breakout_lookback: int = 20
    # exits (start at 100/100 — the only combo with gross edge in the sweep)
    tp_bps: Decimal = Decimal("100")
    sl_bps: Decimal = Decimal("100")
    # volatility floor
    atr_period: int = 14
    atr_min_bps: Decimal = Decimal("15")
    require_vol: bool = True
    # fair value gap
    fvg_lookback: int = 10
    require_fvg: bool = True
    # liquidity sweep
    swing_lookback: int = 20
    require_sweep: bool = False
    # session
    killzone_hours_utc: frozenset[int] = _DEFAULT_KILLZONE_HOURS
    require_session: bool = True


def required_bars(config: IctConfig) -> int:
    """Minimum closed bars before ``evaluate_ict`` is meaningful."""
    return max(
        config.sma_slow,
        config.breakout_lookback + 1,
        config.atr_period + 1,
        config.fvg_lookback + 2,
        config.swing_lookback + 1,
    )


def _no_entry(reason: str) -> SignalDecision:
    return SignalDecision(
        has_entry=False, side=None, entry_price=None, tp_price=None,
        sl_price=None, confidence=Decimal("0"), reason_codes=(reason,),
    )


def _sma(closes: Sequence[Decimal], period: int) -> Decimal:
    window = closes[-period:]
    return sum(window, Decimal("0")) / Decimal(len(window))


def atr_bps(candles: Sequence[Candle], period: int) -> Decimal:
    """Average True Range over the last ``period`` bars, in bps of last close."""
    trs: list[Decimal] = []
    for i in range(len(candles) - period, len(candles)):
        cur = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev_close),
            abs(cur.low - prev_close),
        )
        trs.append(tr)
    atr = sum(trs, Decimal("0")) / Decimal(len(trs))
    last_close = candles[-1].close
    return atr / last_close * _BPS if last_close else Decimal("0")


def has_bullish_fvg(candles: Sequence[Candle], lookback: int) -> bool:
    """Bullish fair value gap in the last ``lookback`` bars: a 3-bar imbalance
    where ``candles[i].high < candles[i+2].low`` (gap up left unfilled)."""
    window = candles[-(lookback + 2):]
    return any(
        window[i].high < window[i + 2].low for i in range(len(window) - 2)
    )


def swept_low_reclaim(candles: Sequence[Candle], lookback: int) -> bool:
    """Sell-side liquidity sweep + reclaim: the current bar's low dips below the
    prior ``lookback``-bar min low (stops taken) but it CLOSES back above it."""
    prior = candles[-(lookback + 1):-1]
    prior_min_low = min(c.low for c in prior)
    cur = candles[-1]
    return cur.low < prior_min_low and cur.close > prior_min_low


def _hour_utc(close_time_ms: int) -> int:
    return (close_time_ms // _MS_PER_HOUR) % 24


def evaluate_ict(candles: Sequence[Candle], config: IctConfig) -> SignalDecision:
    """Long-only ICT-filtered breakout. Pure over the closed-candle sequence."""
    if len(candles) < required_bars(config):
        return _no_entry("INSUFFICIENT_HISTORY")

    closes = [c.close for c in candles]
    current = candles[-1]
    prior = candles[:-1]

    # session filter
    if config.require_session and _hour_utc(current.close_time_ms) not in config.killzone_hours_utc:
        return _no_entry("OUT_OF_SESSION")

    # volatility floor
    atr = atr_bps(candles, config.atr_period)
    if config.require_vol and atr < config.atr_min_bps:
        return _no_entry("LOW_VOLATILITY")

    # trend + breakout confirmation
    sma_fast = _sma(closes, config.sma_fast)
    sma_slow = _sma(closes, config.sma_slow)
    prior_high = max(c.high for c in prior[-config.breakout_lookback:])
    if not (sma_fast > sma_slow and current.close > prior_high):
        return _no_entry("NO_BREAKOUT")

    # imbalance (FVG)
    if config.require_fvg and not has_bullish_fvg(candles, config.fvg_lookback):
        return _no_entry("NO_FVG")

    # liquidity sweep (optional)
    if config.require_sweep and not swept_low_reclaim(candles, config.swing_lookback):
        return _no_entry("NO_SWEEP")

    entry = current.close
    # confidence: scaled by how far ATR exceeds the floor (capped at 1).
    conf = min(Decimal("1"), atr / (config.atr_min_bps * Decimal("4"))) if config.atr_min_bps else Decimal("0.5")
    return SignalDecision(
        has_entry=True,
        side="BUY",
        entry_price=entry,
        tp_price=entry * (Decimal("1") + config.tp_bps / _BPS),
        sl_price=entry * (Decimal("1") - config.sl_bps / _BPS),
        confidence=conf,
        reason_codes=("ICT_LONG", "BREAKOUT", "FVG" if config.require_fvg else "NO_FVG_REQ"),
    )
