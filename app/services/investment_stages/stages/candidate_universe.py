"""Deterministic candidate_universe stage (ROB-279)."""

from __future__ import annotations

from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


class CandidateUniverseStage:
    stage_type = "candidate_universe"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("candidate_universe")
        if not snapshots:
            raise UnavailableStageError("candidate_universe snapshot missing")
        snap = snapshots[0]
        candidates = (snap.payload_json or {}).get("candidates", [])
        top = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)[:5]

        if not top:
            verdict = StageVerdict.NEUTRAL
            confidence = 20
            summary = "no candidates returned by screener"
        elif top[0].get("score", 0.0) >= 7.0:
            verdict = StageVerdict.BULL
            confidence = min(40 + len(top) * 8, 75)
            summary = "top candidates: " + ", ".join(c.get("symbol", "?") for c in top)
        else:
            verdict = StageVerdict.NEUTRAL
            confidence = 35
            summary = "candidates present but low score"

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            key_points=[
                f"{c.get('symbol', '?')} (score={c.get('score', 0):.1f}): {c.get('reason', '')}"
                for c in top
            ],
            buy_evidence=[c.get("symbol", "?") for c in top] if verdict == StageVerdict.BULL else [],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="candidate_universe",
                    payload_path="$.candidates",
                )
            ],
        )
