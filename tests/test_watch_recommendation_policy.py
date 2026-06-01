"""ROB-337 Slice 1 — watch recommendation schema + deterministic policy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.investment_reports import (
    WatchInvalidation,
    WatchPriceRange,
    WatchRecommendationEvidence,
    WatchRecommendationPayload,
)
from app.services.investment_reports.watch_recommendation_policy import (
    LOOKBACK_DAYS,
    POLICY_VERSION,
    VOL_FLOOR,
    WatchPolicyInput,
    compute_watch_recommendation,
)

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


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
            computed_at=datetime(2026, 6, 1, tzinfo=UTC),
            # entry_review_below_price etc. missing -> reject
        )


def test_payload_data_gap_allows_null_prices() -> None:
    payload = WatchRecommendationPayload(
        watch_reason="insufficient daily candles",
        data_state="data_gap",
        source_evidence=_evidence(),
        policy_version="v1",
        computed_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert payload.entry_review_below_price is None
    assert payload.data_state == "data_gap"


def _flat_input(n: int = 25, price: str = "100") -> WatchPolicyInput:
    p = Decimal(price)
    return WatchPolicyInput(
        reference_price=p,
        best_bid=None,
        best_ask=None,
        daily_highs=[p] * n,
        daily_lows=[p] * n,
        daily_closes=[p] * n,
    )


def test_policy_flat_series_exact() -> None:
    rec = compute_watch_recommendation(_flat_input(), computed_at=_NOW)
    assert rec.data_state == "ok"
    assert rec.policy_version == POLICY_VERSION
    assert rec.source_evidence.volatility_pct == VOL_FLOOR  # 0.02 floor
    assert rec.source_evidence.support == Decimal("100")
    # raw_entry=98, support_floor=100.5 -> clamp to reference 100
    assert rec.entry_review_below_price == Decimal("100")
    assert rec.max_chase_price == Decimal("100")  # min(100, 100*1.005)
    assert rec.invalidation.kind == "price_below"
    assert rec.invalidation.price == Decimal("98.000")  # 100*(1-0.02)
    # range collapses (low 100.5 > high 100 -> low=high=high)
    assert rec.suggested_limit_price_range.low == rec.suggested_limit_price_range.high


def test_policy_support_below_price_inequalities() -> None:
    # support=80, resistance=115, varied -> vol>floor
    lows = [
        Decimal(x)
        for x in [
            95,
            92,
            90,
            88,
            85,
            83,
            80,
            82,
            84,
            86,
            88,
            90,
            91,
            89,
            87,
            85,
            83,
            84,
            86,
            88,
            90,
            92,
            94,
            95,
            96,
        ]
    ]
    highs = [low + Decimal("15") for low in lows]
    closes = [low + Decimal("5") for low in lows]
    inp = WatchPolicyInput(
        reference_price=Decimal("100"),
        best_bid=None,
        best_ask=None,
        daily_highs=highs,
        daily_lows=lows,
        daily_closes=closes,
    )
    rec = compute_watch_recommendation(inp, computed_at=_NOW)
    assert rec.data_state == "ok"
    assert rec.source_evidence.support == Decimal("80")
    assert rec.entry_review_below_price < Decimal("100")  # below current
    assert rec.suggested_limit_price_range.low <= rec.suggested_limit_price_range.high
    assert rec.max_chase_price <= Decimal("100")  # no chase above current
    assert rec.invalidation.price < Decimal("80")  # below support


def test_policy_data_gap_when_too_few_candles() -> None:
    inp = WatchPolicyInput(
        reference_price=Decimal("100"),
        best_bid=None,
        best_ask=None,
        daily_highs=[Decimal("100")] * 10,
        daily_lows=[Decimal("100")] * 10,
        daily_closes=[Decimal("100")] * 10,
    )
    rec = compute_watch_recommendation(inp, computed_at=_NOW)
    assert rec.data_state == "data_gap"
    assert rec.entry_review_below_price is None
    assert rec.max_chase_price is None
    assert rec.invalidation is None
    assert rec.source_evidence.lookback_days == LOOKBACK_DAYS


def test_policy_expiry_uses_valid_until_when_given() -> None:
    vu = datetime(2026, 6, 20, tzinfo=UTC)
    rec = compute_watch_recommendation(_flat_input(), computed_at=_NOW, valid_until=vu)
    assert rec.expiry_at == vu
