"""ROB-337 Slice 1 — watch recommendation schema + deterministic policy."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import pytest

from app.schemas.investment_reports import (
    WatchInvalidation,
    WatchPriceRange,
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)


def _evidence() -> WatchRecommendationEvidence:
    return WatchRecommendationEvidence(lookback_days=20)


def test_price_range_rejects_low_above_high() -> None:
    with pytest.raises(ValueError):
        WatchPriceRange(low=Decimal("10"), high=Decimal("9"))


def test_invalidation_price_below_requires_price() -> None:
    with pytest.raises(ValueError):
        WatchInvalidation(kind="price_below")


def test_invalidation_condition_text_requires_text() -> None:
    with pytest.raises(ValueError):
        WatchInvalidation(kind="condition_text")


def test_payload_ok_requires_price_fields() -> None:
    with pytest.raises(ValueError):
        WatchRecommendationPayload(
            watch_reason="r",
            data_state="ok",
            source_evidence=_evidence(),
            policy_version="v1",
            computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            # entry_review_below_price etc. missing -> reject
        )


def test_payload_data_gap_allows_null_prices() -> None:
    payload = WatchRecommendationPayload(
        watch_reason="insufficient daily candles",
        data_state="data_gap",
        source_evidence=_evidence(),
        policy_version="v1",
        computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert payload.entry_review_below_price is None
    assert payload.data_state == "data_gap"
