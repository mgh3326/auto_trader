from datetime import datetime, UTC
import logging
import statistics
from typing import Any

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.mcp_server.tooling.fundamentals_sources_naver import _fetch_sector_peers_naver
from app.mcp_server.tooling.fundamentals_sources_yfinance import _fetch_sector_peers_us
from app.schemas.research_pipeline import (
    FundamentalsSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)

logger = logging.getLogger(__name__)


async def _fetch_fundamentals(symbol: str, instrument_type: str) -> dict[str, Any]:
    """Fetch fundamental data and peers for comparison."""
    if instrument_type == "equity_kr":
        return await _fetch_sector_peers_naver(symbol, limit=10)
    elif instrument_type == "equity_us":
        return await _fetch_sector_peers_us(symbol, limit=10)
    else:
        raise ValueError(f"Fundamentals analysis not supported for {instrument_type}")


class FundamentalsStageAnalyzer(BaseStageAnalyzer):
    stage_type = "fundamentals"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        if ctx.instrument_type not in ("equity_kr", "equity_us"):
            return StageOutput(
                stage_type=self.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                signals=FundamentalsSignals(),
            )

        try:
            raw = await _fetch_fundamentals(ctx.symbol, ctx.instrument_type)
        except Exception as exc:
            logger.error(f"Fundamentals analysis failed for {ctx.symbol}: {exc}")
            return StageOutput(
                stage_type=self.stage_type,
                verdict=StageVerdict.UNAVAILABLE,
                confidence=0,
                signals=FundamentalsSignals(),
            )

        target_per = raw.get("per")
        peers = raw.get("peers", [])
        
        all_pers = []
        if target_per is not None and target_per > 0:
            all_pers.append(target_per)
        for p in peers:
            p_per = p.get("per")
            if p_per is not None and p_per > 0:
                all_pers.append(p_per)

        median_per = None
        relative_per = None
        if all_pers:
            median_per = statistics.median(all_pers)
            if target_per is not None and target_per > 0 and median_per > 0:
                relative_per = round(target_per / median_per, 3)

        signals = FundamentalsSignals(
            per=target_per,
            pbr=raw.get("pbr"),
            market_cap=raw.get("market_cap"),
            sector=raw.get("sector"),
            peer_count=len(peers),
            relative_per_vs_peers=relative_per,
        )

        # Verdict mapping rule:
        # BULL: per below sector median by >20% (relative_per < 0.8)
        # BEAR: per above sector median by >50% (relative_per > 1.5)
        # NEUTRAL: otherwise
        verdict = StageVerdict.NEUTRAL
        confidence = 0
        
        if relative_per is not None:
            confidence = 70
            if relative_per < 0.8:
                verdict = StageVerdict.BULL
            elif relative_per > 1.5:
                verdict = StageVerdict.BEAR
        else:
            # If we have PER but no peers, or no PER at all
            if target_per is None:
                verdict = StageVerdict.UNAVAILABLE
            else:
                verdict = StageVerdict.NEUTRAL
                confidence = 30

        return StageOutput(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            signals=signals,
            snapshot_at=datetime.now(UTC),
            source_freshness=SourceFreshness(
                newest_age_minutes=0,
                oldest_age_minutes=0,
                source_count=1,
            ),
        )
