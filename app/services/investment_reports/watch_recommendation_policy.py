"""ROB-337 Slice 1 — deterministic watch recommendation policy.

Pure functions: given injected market evidence (reference price + daily
OHLC arrays, ordered oldest->newest), compute advisory buy-review price
thresholds. No DB / network access; ``computed_at`` is injected so the
output is fully deterministic and unit-testable. Advisory only — never
produces an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.schemas.investment_reports import (
    WatchInvalidation,
    WatchPriceRange,
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)

POLICY_VERSION = "v1"
LOOKBACK_DAYS = 20
ATR_PERIOD = 14
VOL_FLOOR = Decimal("0.02")
K_ENTRY = Decimal("1.0")
SUPPORT_BUFFER = Decimal("0.005")
CHASE_BUFFER = Decimal("0.005")
INVAL_FLOOR = Decimal("0.02")
DEFAULT_HORIZON_DAYS = 14


@dataclass(frozen=True)
class WatchPolicyInput:
    """Evidence for the policy. OHLC lists ordered oldest->newest."""

    reference_price: Decimal | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    daily_highs: list[Decimal]
    daily_lows: list[Decimal]
    daily_closes: list[Decimal]
    news_ref: str | None = None
    screener_reason: str | None = None


def _atr_pct(inp: WatchPolicyInput) -> Decimal:
    """ATR(14)/reference_price, floored at VOL_FLOOR. Inputs are guaranteed
    long enough by the data_gap gate (LOOKBACK_DAYS > ATR_PERIOD)."""
    highs = inp.daily_highs[-(ATR_PERIOD + 1):]
    lows = inp.daily_lows[-(ATR_PERIOD + 1):]
    closes = inp.daily_closes[-(ATR_PERIOD + 1):]
    trs: list[Decimal] = []
    for i in range(1, len(highs)):
        prev_close = closes[i - 1]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
    if not trs or inp.reference_price in (None, Decimal("0")):
        return VOL_FLOOR
    atr = sum(trs, Decimal("0")) / Decimal(len(trs))
    pct = atr / inp.reference_price
    return max(pct, VOL_FLOOR)


def _is_data_gap(inp: WatchPolicyInput) -> bool:
    return (
        inp.reference_price is None
        or len(inp.daily_lows) < LOOKBACK_DAYS
        or len(inp.daily_highs) < LOOKBACK_DAYS
        or len(inp.daily_closes) < LOOKBACK_DAYS
    )


def compute_watch_recommendation(
    inp: WatchPolicyInput,
    *,
    computed_at: datetime,
    valid_until: datetime | None = None,
    watch_reason_prefix: str = "dip-buy review",
) -> WatchRecommendationPayload:
    if _is_data_gap(inp):
        return WatchRecommendationPayload(
            watch_reason=(
                f"data_gap: need >= {LOOKBACK_DAYS} daily candles and a "
                "reference price to compute thresholds"
            ),
            data_state="data_gap",
            source_evidence=WatchRecommendationEvidence(
                lookback_days=LOOKBACK_DAYS,
                news_ref=inp.news_ref,
                screener_reason=inp.screener_reason,
            ),
            policy_version=POLICY_VERSION,
            computed_at=computed_at,
            expiry_at=valid_until,
        )

    reference_price = inp.reference_price
    assert reference_price is not None  # guarded by _is_data_gap
    support = min(inp.daily_lows[-LOOKBACK_DAYS:])
    resistance = max(inp.daily_highs[-LOOKBACK_DAYS:])
    vol = _atr_pct(inp)

    support_floor = support * (Decimal("1") + SUPPORT_BUFFER)
    raw_entry = reference_price * (Decimal("1") - K_ENTRY * vol)
    entry = min(max(raw_entry, support_floor), reference_price)

    range_low = support_floor
    range_high = entry
    if range_low > range_high:
        range_low = range_high

    max_chase = min(reference_price, entry * (Decimal("1") + CHASE_BUFFER))
    inval_price = support * (Decimal("1") - max(vol, INVAL_FLOOR))

    expiry = valid_until or (computed_at + timedelta(days=DEFAULT_HORIZON_DAYS))

    return WatchRecommendationPayload(
        watch_reason=(
            f"{watch_reason_prefix}: review below {entry} toward {support_floor} "
            f"support; vol {vol} (frac); invalid below {inval_price}"
        ),
        data_state="ok",
        reference_price=reference_price,
        entry_review_below_price=entry,
        suggested_limit_price_range=WatchPriceRange(low=range_low, high=range_high),
        max_chase_price=max_chase,
        invalidation=WatchInvalidation(kind="price_below", price=inval_price),
        expiry_at=expiry,
        review_cadence="daily",
        source_evidence=WatchRecommendationEvidence(
            support=support,
            resistance=resistance,
            spread_bps=None,  # v1: Quote has no bid/ask; orderbook fetch deferred
            volatility_pct=vol,
            lookback_days=LOOKBACK_DAYS,
            news_ref=inp.news_ref,
            screener_reason=inp.screener_reason,
        ),
        policy_version=POLICY_VERSION,
        computed_at=computed_at,
    )
