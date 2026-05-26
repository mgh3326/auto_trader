"""ROB-320 — deterministic z-score mean-reversion fade signal (pure, testable).

Non-micro-breakout alpha candidate. Hypothesis: after a short-horizon price
extension away from its rolling mean, price reverts. We FADE the extension
(buy dips stretched below the band) — the opposite of the ROB-307 breakout
signal which CHASES extension. Every rule is a pure function of a closed-candle
sequence: no chart reading, no lookahead, no network, no volume dependency
(stays within the existing ``Candle`` contract so the Nautilus bridge and the
backtest/gate harness reuse unchanged).

Spot is long-only (fade oversold dips); the futures short mirror (fade
overbought spikes) is gated on ``allow_short`` exactly like the production
breakout signal.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ict_signal import atr_bps  # pure, reused (DRY)

from app.services.brokers.binance.demo_scalping.signal import Candle, SignalDecision

_BPS = Decimal("10000")


@dataclass(frozen=True)
class MeanRevConfig:
    lookback: int = 20
    z_entry: Decimal = Decimal("2.0")   # enter when z crosses +/- this
    tp_bps: Decimal = Decimal("30")     # revert target
    sl_bps: Decimal = Decimal("30")
    atr_period: int = 14
    atr_min_bps: Decimal = Decimal("8")
    require_vol: bool = True
    allow_short: bool = False           # spot: False; futures: True


def required_bars(config: MeanRevConfig) -> int:
    return max(config.lookback, config.atr_period + 1)


def _mean(xs: Sequence[Decimal]) -> Decimal:
    return sum(xs, Decimal("0")) / Decimal(len(xs))


def _pop_stddev(xs: Sequence[Decimal]) -> Decimal:
    m = _mean(xs)
    var = sum(((x - m) ** 2 for x in xs), Decimal("0")) / Decimal(len(xs))
    return var.sqrt()


def zscore(closes: Sequence[Decimal], lookback: int) -> Decimal:
    """z of the last close vs the rolling window mean/stddev. 0 if no dispersion."""
    window = closes[-lookback:]
    sd = _pop_stddev(window)
    if sd == 0:
        return Decimal("0")
    return (window[-1] - _mean(window)) / sd


def _no_entry(reason: str) -> SignalDecision:
    return SignalDecision(
        has_entry=False, side=None, entry_price=None, tp_price=None,
        sl_price=None, confidence=Decimal("0"), reason_codes=(reason,),
    )


def evaluate_meanrev(candles: Sequence[Candle], config: MeanRevConfig) -> SignalDecision:
    """Long-only (spot) / short-mirror (futures) z-score fade. Pure over closed candles."""
    if len(candles) < required_bars(config):
        return _no_entry("INSUFFICIENT_HISTORY")

    if config.require_vol and atr_bps(candles, config.atr_period) < config.atr_min_bps:
        return _no_entry("LOW_VOLATILITY")

    closes = [c.close for c in candles]
    z = zscore(closes, config.lookback)
    if z == 0:
        return _no_entry("NO_DISPERSION")

    current = candles[-1]
    entry = current.close
    conf = min(Decimal("1"), (abs(z) - config.z_entry) / config.z_entry) if config.z_entry else Decimal("0.5")
    conf = max(Decimal("0"), conf)

    long_ok = z <= -config.z_entry
    short_ok = config.allow_short and z >= config.z_entry

    if long_ok:
        return SignalDecision(
            has_entry=True, side="BUY", entry_price=entry,
            tp_price=entry * (Decimal("1") + config.tp_bps / _BPS),
            sl_price=entry * (Decimal("1") - config.sl_bps / _BPS),
            confidence=conf, reason_codes=("MEANREV_LONG", "OVERSOLD_FADE"),
        )
    if short_ok:
        return SignalDecision(
            has_entry=True, side="SELL", entry_price=entry,
            tp_price=entry * (Decimal("1") - config.tp_bps / _BPS),
            sl_price=entry * (Decimal("1") + config.sl_bps / _BPS),
            confidence=conf, reason_codes=("MEANREV_SHORT", "OVERBOUGHT_FADE"),
        )
    return _no_entry("WITHIN_BAND")
