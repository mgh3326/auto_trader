import pytest
from pydantic import ValidationError

from app.schemas.research_pipeline import (
    MarketSignals,
    NewsSignals,
    FundamentalsSignals,
    SocialSignals,
    SourceFreshness,
    StageVerdict,
)


@pytest.mark.unit
def test_market_signals_valid():
    sig = MarketSignals(
        last_close=12345.0,
        change_pct=1.23,
        rsi_14=55.0,
        atr_14=410.5,
        volume_ratio_20d=1.8,
        trend="uptrend",
    )
    assert sig.last_close == 12345.0


@pytest.mark.unit
def test_market_signals_rejects_out_of_range_rsi():
    with pytest.raises(ValidationError):
        MarketSignals(last_close=1.0, change_pct=0.0, rsi_14=120.0,
                      atr_14=0.1, volume_ratio_20d=1.0, trend="flat")


@pytest.mark.unit
def test_social_signals_placeholder_shape():
    sig = SocialSignals(available=False, reason="not_implemented", phase="placeholder")
    assert sig.available is False
    assert sig.reason == "not_implemented"


@pytest.mark.unit
def test_source_freshness_required_keys():
    fresh = SourceFreshness(
        newest_age_minutes=5,
        oldest_age_minutes=120,
        missing_sources=[],
        stale_flags=[],
        source_count=3,
    )
    assert fresh.source_count == 3


@pytest.mark.unit
def test_stage_verdict_enum():
    assert {v.value for v in StageVerdict} == {"bull", "bear", "neutral", "unavailable"}
