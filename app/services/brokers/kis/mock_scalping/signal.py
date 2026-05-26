"""ROB-321 PR3 — deterministic long-only trend micro-breakout scalping signal.

Mirrors the demo_scalping signal pattern (ROB-307) but for KR cash equities:

* **Long-only.** Enter when ``sma_fast > sma_slow`` (uptrend) AND the latest
  close breaks above the prior ``breakout_lookback``-bar high. There is no short
  branch — KR cash equities cannot be shorted here.
* **No-chase guard** (ROB-321 §3 "당일 급등주 추격 금지"): if the breakout margin
  above the prior high already exceeds ``max_chase_bps``, the candle has run too
  far — reject with ``CHASE_TOO_FAR`` rather than chase a spike.
* Exits are fixed-bps TP/SL relative to the entry candidate.

``evaluate_signal`` is pure over a candle sequence — deterministic, no network,
no broker, no LLM. Thresholds live in ``SignalConfig`` so they are trivially
tunable and test-pinned. (Edge is intentionally a conservative toy: ROB-316
showed scalping is net-negative after fees; this layer validates the plumbing.)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.services.brokers.kis.mock_scalping.contract import ReasonCode, Side

_BPS = Decimal("10000")
# Reference separation/margin (in bps) that maps to full confidence.
_CONFIDENCE_REFERENCE_BPS = Decimal("50")


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    close_time_ms: int


@dataclass(frozen=True)
class SignalConfig:
    sma_fast: int = 7
    sma_slow: int = 25
    breakout_lookback: int = 20
    tp_bps: Decimal = Decimal("30")
    sl_bps: Decimal = Decimal("20")
    # No-chase ceiling: reject a breakout that already ran more than this many
    # bps above the prior high (don't chase 급등주).
    max_chase_bps: Decimal = Decimal("50")


@dataclass(frozen=True)
class SignalDecision:
    has_entry: bool
    side: Side | None
    entry_price: Decimal | None
    tp_price: Decimal | None
    sl_price: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


def _sma(closes: Sequence[Decimal], period: int) -> Decimal:
    window = closes[-period:]
    return sum(window, Decimal("0")) / Decimal(len(window))


def _clamp01(value: Decimal) -> Decimal:
    if value < 0:
        return Decimal("0")
    if value > 1:
        return Decimal("1")
    return value


def _no_entry(reason: str) -> SignalDecision:
    return SignalDecision(
        has_entry=False,
        side=None,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        confidence=Decimal("0"),
        reason_codes=(reason,),
    )


def _confidence(*, separation_bps: Decimal, margin_bps: Decimal) -> Decimal:
    sep_norm = _clamp01(abs(separation_bps) / _CONFIDENCE_REFERENCE_BPS)
    margin_norm = _clamp01(abs(margin_bps) / _CONFIDENCE_REFERENCE_BPS)
    return _clamp01((sep_norm + margin_norm) / Decimal("2"))


def evaluate_signal(candles: Sequence[Candle], config: SignalConfig) -> SignalDecision:
    """Evaluate the latest candle against the long-only micro-breakout rule."""

    needed = max(config.sma_slow, config.breakout_lookback + 1)
    if len(candles) < needed:
        return _no_entry(ReasonCode.INSUFFICIENT_HISTORY)

    closes = [c.close for c in candles]
    current = candles[-1]
    prior = candles[:-1]

    sma_fast = _sma(closes, config.sma_fast)
    sma_slow = _sma(closes, config.sma_slow)
    separation_bps = (sma_fast - sma_slow) / sma_slow * _BPS

    lookback = prior[-config.breakout_lookback :]
    prior_high = max(c.high for c in lookback)

    if not (sma_fast > sma_slow and current.close > prior_high):
        return _no_entry(ReasonCode.NO_SIGNAL)

    entry = current.close
    margin_bps = (entry - prior_high) / prior_high * _BPS

    # No-chase guard: the breakout already ran too far above the prior high.
    if margin_bps > config.max_chase_bps:
        return _no_entry(ReasonCode.CHASE_TOO_FAR)

    return SignalDecision(
        has_entry=True,
        side="BUY",
        entry_price=entry,
        tp_price=entry * (Decimal("1") + config.tp_bps / _BPS),
        sl_price=entry * (Decimal("1") - config.sl_bps / _BPS),
        confidence=_confidence(separation_bps=separation_bps, margin_bps=margin_bps),
        reason_codes=(ReasonCode.ENTER_LONG_BREAKOUT,),
    )
