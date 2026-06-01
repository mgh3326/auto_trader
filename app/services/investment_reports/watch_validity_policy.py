"""ROB-337 Slice 2 — deterministic watch validity classifier.

Pure function: given the stored watch_recommendation, current price, a
fresh recompute, and valid_until/now, classify whether an active watch is
still meaningful. No I/O. Advisory only — never mutates or orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from app.schemas.investment_reports import WatchRecommendationPayload

WatchValidityVerdict = Literal["keep", "reprice", "expire", "review_now", "data_gap"]

EXPIRE_SOON_DAYS = 2
REPRICE_DRIFT_PCT = Decimal("0.05")


@dataclass(frozen=True)
class WatchValidityInput:
    stored_recommendation: dict | None
    current_price: Decimal | None
    recomputed: WatchRecommendationPayload | None
    valid_until: datetime | None
    now: datetime


@dataclass(frozen=True)
class WatchValidityResult:
    verdict: WatchValidityVerdict
    reason: str
    recomputed: WatchRecommendationPayload | None
    signals: dict[str, Any]


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def classify_watch_validity(inp: WatchValidityInput) -> WatchValidityResult:
    stored = inp.stored_recommendation or {}
    price = inp.current_price
    entry = _dec(stored.get("entry_review_below_price"))
    inval = stored.get("invalidation") or {}
    inval_price = (
        _dec(inval.get("price")) if inval.get("kind") == "price_below" else None
    )
    days_to_expiry = None
    if inp.valid_until is not None:
        days_to_expiry = (inp.valid_until - inp.now).total_seconds() / 86400.0

    signals: dict[str, Any] = {
        "current_price": str(price) if price is not None else None,
        "entry_review_below_price": str(entry) if entry is not None else None,
        "invalidation_price": str(inval_price) if inval_price is not None else None,
        "days_to_expiry": days_to_expiry,
    }

    recomputed_gap = (
        inp.recomputed is not None and inp.recomputed.data_state == "data_gap"
    )
    if price is None or (not stored and recomputed_gap):
        return WatchValidityResult(
            verdict="data_gap",
            reason=(
                "no current price"
                if price is None
                else "no stored recommendation and recompute returned data_gap"
            ),
            recomputed=None,
            signals=signals,
        )

    if inval_price is not None and price < inval_price:
        return WatchValidityResult(
            verdict="expire",
            reason=f"price {price} fell below invalidation {inval_price} (thesis broken)",
            recomputed=None,
            signals=signals,
        )
    if days_to_expiry is not None and days_to_expiry <= EXPIRE_SOON_DAYS:
        return WatchValidityResult(
            verdict="expire",
            reason=f"expires in {days_to_expiry:.2f} days (<= {EXPIRE_SOON_DAYS})",
            recomputed=None,
            signals=signals,
        )

    if entry is not None and price <= entry:
        return WatchValidityResult(
            verdict="review_now",
            reason=f"price {price} entered review zone (<= {entry})",
            recomputed=None,
            signals=signals,
        )

    if inp.recomputed is not None and inp.recomputed.data_state == "ok":
        new_entry = inp.recomputed.entry_review_below_price
        if entry is None:
            return WatchValidityResult(
                verdict="reprice",
                reason="no stored thresholds; recommendation available to populate",
                recomputed=inp.recomputed,
                signals=signals,
            )
        if new_entry is not None and entry != 0:
            drift = abs(new_entry - entry) / entry
            signals["drift_pct"] = str(drift)
            if drift > REPRICE_DRIFT_PCT:
                return WatchValidityResult(
                    verdict="reprice",
                    reason=f"entry drift {drift} > {REPRICE_DRIFT_PCT}",
                    recomputed=inp.recomputed,
                    signals=signals,
                )

    return WatchValidityResult(
        verdict="keep",
        reason="still valid; price above review zone, thesis intact",
        recomputed=None,
        signals=signals,
    )
