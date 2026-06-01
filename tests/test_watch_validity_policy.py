"""ROB-337 Slice 2 — watch validity classifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.investment_reports import (
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)
from app.services.investment_reports.watch_validity_policy import (
    WatchValidityInput,
    classify_watch_validity,
)

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _stored(entry="100", inval="80") -> dict:
    return {
        "entry_review_below_price": entry,
        "invalidation": {"kind": "price_below", "price": inval},
    }


def _recomputed_ok(entry: str) -> WatchRecommendationPayload:
    return WatchRecommendationPayload(
        watch_reason="r",
        data_state="ok",
        reference_price=Decimal("100"),
        entry_review_below_price=Decimal(entry),
        suggested_limit_price_range={"low": Decimal(entry), "high": Decimal(entry)},
        max_chase_price=Decimal(entry),
        invalidation={"kind": "price_below", "price": Decimal("70")},
        source_evidence=WatchRecommendationEvidence(lookback_days=20),
        policy_version="v1",
        computed_at=_NOW,
    )


def _inp(**kw) -> WatchValidityInput:
    base = {
        "stored_recommendation": _stored(),
        "current_price": Decimal("90"),
        "recomputed": None,
        "valid_until": _NOW + timedelta(days=30),
        "now": _NOW,
    }
    base.update(kw)
    return WatchValidityInput(**base)


def test_data_gap_when_no_current_price() -> None:
    assert classify_watch_validity(_inp(current_price=None)).verdict == "data_gap"


def test_expire_when_below_invalidation() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("79")))
    assert r.verdict == "expire"
    assert "invalidation" in r.reason


def test_expire_when_near_expiry() -> None:
    r = classify_watch_validity(
        _inp(current_price=Decimal("95"), valid_until=_NOW + timedelta(days=1))
    )
    assert r.verdict == "expire"


def test_review_now_when_in_zone() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("100")))
    assert r.verdict == "review_now"


def test_keep_when_above_zone_and_intact() -> None:
    r = classify_watch_validity(_inp(current_price=Decimal("120")))
    assert r.verdict == "keep"


def test_priority_invalidation_beats_zone() -> None:
    # price below invalidation AND below entry zone -> expire wins
    r = classify_watch_validity(_inp(current_price=Decimal("75")))
    assert r.verdict == "expire"


def test_reprice_on_material_drift() -> None:
    # entry stored 100, recomputed 120 -> drift 20% > 5%; price above zone so not review_now
    r = classify_watch_validity(
        _inp(current_price=Decimal("130"), recomputed=_recomputed_ok("120"))
    )
    assert r.verdict == "reprice"
    assert r.recomputed is not None


def test_no_stored_with_recompute_is_reprice() -> None:
    r = classify_watch_validity(
        _inp(stored_recommendation=None, current_price=Decimal("100"),
             recomputed=_recomputed_ok("90"))
    )
    assert r.verdict == "reprice"
