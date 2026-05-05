from datetime import UTC, datetime

import pytest

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.schemas.research_pipeline import (
    MarketSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)


class _DummyMarketStage(BaseStageAnalyzer):
    stage_type = "market"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        return StageOutput(
            stage_type="market",
            verdict=StageVerdict.NEUTRAL,
            confidence=50,
            signals=MarketSignals(
                last_close=100.0,
                change_pct=0.0,
                rsi_14=50.0,
                atr_14=1.0,
                volume_ratio_20d=1.0,
                trend="flat",
            ),
            source_freshness=SourceFreshness(
                newest_age_minutes=1,
                oldest_age_minutes=1,
                missing_sources=[],
                stale_flags=[],
                source_count=1,
            ),
            snapshot_at=datetime.now(tz=UTC),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dummy_stage_returns_validated_output():
    stage = _DummyMarketStage()
    out = await stage.analyze(
        StageContext(
            session_id=1,
            symbol="005930",
            symbol_name="Samsung",
            instrument_type="equity_kr",
        )
    )

    assert out.stage_type == "market"
    assert isinstance(out.signals, MarketSignals)


@pytest.mark.unit
def test_base_stage_rejects_wrong_stage_type():
    class _Bad(BaseStageAnalyzer):
        stage_type = "market"

        async def analyze(self, ctx):
            return StageOutput(
                stage_type="news",  # mismatch
                verdict=StageVerdict.NEUTRAL,
                confidence=10,
                signals=MarketSignals(
                    last_close=1.0,
                    change_pct=0.0,
                    rsi_14=10.0,
                    atr_14=0.1,
                    volume_ratio_20d=1.0,
                    trend="flat",
                ),
            )

    import asyncio

    stage = _Bad()
    with pytest.raises(ValueError, match="stage_type mismatch"):
        asyncio.run(
            stage.run(
                StageContext(
                    session_id=1,
                    symbol="X",
                    symbol_name="X Corp",
                    instrument_type="equity_kr",
                )
            )
        )

