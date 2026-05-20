"""Deterministic market stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import StageContext, UnavailableStageError

_BULL_THRESHOLD = 0.5
_BEAR_THRESHOLD = -0.5


class MarketStage:
    stage_type = "market"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("market")
        if not snapshots:
            raise UnavailableStageError("market snapshot missing from bundle")

        snapshot = snapshots[0]
        indices = (snapshot.payload_json or {}).get("indices", {})
        kospi = indices.get("KOSPI") or indices.get("kospi") or {}
        change = float(kospi.get("change_percent", 0.0))

        if change >= _BULL_THRESHOLD:
            verdict = StageVerdict.BULL
        elif change <= _BEAR_THRESHOLD:
            verdict = StageVerdict.BEAR
        else:
            verdict = StageVerdict.NEUTRAL

        confidence = min(int(abs(change) * 30), 90)

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=max(confidence, 30 if verdict != StageVerdict.NEUTRAL else 20),
            summary=f"KOSPI change_percent={change:+.2f}%",
            key_points=[f"KOSPI {change:+.2f}%"],
            buy_evidence=[f"KOSPI 상승 {change:+.2f}%"] if verdict == StageVerdict.BULL else [],
            sell_evidence=[f"KOSPI 하락 {change:+.2f}%"] if verdict == StageVerdict.BEAR else [],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snapshot.snapshot_uuid,
                    snapshot_kind="market",
                    payload_path="$.indices.KOSPI.change_percent",
                )
            ],
        )
