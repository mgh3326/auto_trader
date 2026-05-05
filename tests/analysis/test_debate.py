import pytest

from app.analysis.debate import build_summary
from app.schemas.research_pipeline import (
    FundamentalsSignals,
    MarketSignals,
    NewsSignals,
    SocialSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
    SummaryDecision,
)


def create_mock_stage(stage_type, verdict, stale=False):
    signals_map = {
        "market": MarketSignals(
            last_close=100.0,
            change_pct=1.0,
            rsi_14=50.0,
            atr_14=2.0,
            volume_ratio_20d=1.0,
            trend="uptrend",
        ),
        "news": NewsSignals(
            headline_count=5,
            sentiment_score=0.5,
            top_themes=["bullish"],
            urgent_flags=[],
        ),
        "fundamentals": FundamentalsSignals(
            per=15.0,
            pbr=1.5,
            market_cap=1000000,
            sector="Tech",
            peer_count=5,
            relative_per_vs_peers=1.0,
        ),
        "social": SocialSignals(available=True, reason="ok", phase="production"),
    }

    stale_flags = ["price"] if stale else []

    return StageOutput(
        stage_type=stage_type,
        verdict=verdict,
        confidence=80,
        signals=signals_map[stage_type],
        source_freshness=SourceFreshness(
            newest_age_minutes=5,
            oldest_age_minutes=10,
            missing_sources=[],
            stale_flags=stale_flags,
            source_count=1,
        ),
    )


@pytest.mark.asyncio
async def test_build_summary_deterministic_buy():
    # 2 bull, 1 neutral, 1 unavailable -> BUY
    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.BULL),
        102: create_mock_stage("news", StageVerdict.BULL),
        103: create_mock_stage("fundamentals", StageVerdict.NEUTRAL),
        104: create_mock_stage("social", StageVerdict.UNAVAILABLE),
    }

    summary, links = await build_summary(stage_outputs)

    assert summary.decision == SummaryDecision.BUY
    assert len(summary.bull_arguments) > 0
    # Bull arguments must cite at least one stage
    for arg in summary.bull_arguments:
        assert len(arg.cited_stage_ids) > 0
        assert all(cid in [101, 102] for cid in arg.cited_stage_ids)

    assert any("social: UNAVAILABLE" in w for w in summary.warnings)


@pytest.mark.asyncio
async def test_build_summary_stale_to_hold():
    # 2 stale stages -> force HOLD
    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.BULL, stale=True),
        102: create_mock_stage("news", StageVerdict.BULL, stale=True),
        103: create_mock_stage("fundamentals", StageVerdict.BULL),
        104: create_mock_stage("social", StageVerdict.BULL),
    }

    summary, links = await build_summary(stage_outputs)

    assert summary.decision == SummaryDecision.HOLD
    assert any("stale" in w.lower() for w in summary.warnings)


@pytest.mark.asyncio
async def test_build_summary_citation_invariant():
    # Bull/bear arguments must each cite at least one stage_analysis id
    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.BULL),
        102: create_mock_stage("news", StageVerdict.BEAR),
    }

    summary, links = await build_summary(stage_outputs)

    if summary.bull_arguments:
        assert all(len(arg.cited_stage_ids) > 0 for arg in summary.bull_arguments)
    if summary.bear_arguments:
        assert all(len(arg.cited_stage_ids) > 0 for arg in summary.bear_arguments)


@pytest.mark.asyncio
async def test_build_summary_unavailable_warning():
    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.UNAVAILABLE),
    }
    summary, links = await build_summary(stage_outputs)
    assert any("market: UNAVAILABLE" in w for w in summary.warnings)


@pytest.mark.asyncio
async def test_build_summary_llm_path():
    async def mock_runner(prompt, **kwargs):
        return {"decision": "buy"}

    stage_outputs = {
        101: create_mock_stage("market", StageVerdict.BULL),
    }

    summary, links = await build_summary(stage_outputs, model_runner=mock_runner)

    assert summary.model_name == "mock-llm"
    assert summary.raw_payload == {"simulation": True}
    assert summary.token_input == 100
    assert summary.token_output == 50
