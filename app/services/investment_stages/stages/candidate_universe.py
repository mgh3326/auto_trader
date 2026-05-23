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

_FRESHNESS_CAP = {"fresh": 100, "partial": 60, "stale": 40, "missing": 20}


def _cap_confidence(base: int, freshness_status: str, source_count: int) -> int:
    cap = _FRESHNESS_CAP.get(freshness_status, 40)
    confidence = min(base, cap)
    if source_count <= 1:
        confidence = min(confidence, 65)
    return confidence


class CandidateUniverseStage:
    stage_type = "candidate_universe"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("candidate_universe")
        if not snapshots:
            raise UnavailableStageError("candidate_universe snapshot missing")
        snap = snapshots[0]
        payload = snap.payload_json or {}
        candidates = payload.get("candidates", [])
        freshness_status = payload.get("freshness_status", "missing")
        source_coverage = payload.get("source_coverage", {}) or {}
        missing = payload.get("missing_data")

        top = sorted(candidates, key=lambda c: c.get("score", 0.0), reverse=True)[:5]

        if not top:
            verdict = StageVerdict.NEUTRAL
            base = 20
            summary = "스크리너 후보 없음"
        elif top[0].get("score", 0.0) >= 7.0:
            verdict = StageVerdict.BULL
            base = min(40 + len(top) * 8, 75)
            summary = "상위 후보: " + ", ".join(c.get("symbol", "?") for c in top)
        else:
            verdict = StageVerdict.NEUTRAL
            base = 35
            summary = "후보는 있으나 점수 낮음"

        confidence = _cap_confidence(base, freshness_status, len(source_coverage))

        key_points = [
            f"{c.get('symbol', '?')} (score={c.get('score', 0):.1f}): "
            f"{', '.join(c.get('reasons', []))} [{c.get('source', '?')}]"
            for c in top
        ]
        missing_lines: list[str] = []
        freshness_summary = None
        if missing:
            missing_lines = [missing.get("what", ""), missing.get("why", "")]
            missing_lines = [m for m in missing_lines if m]
            freshness_summary = {"candidate_universe": missing}

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=summary,
            key_points=key_points,
            buy_evidence=[c.get("symbol", "?") for c in top]
            if verdict == StageVerdict.BULL
            else [],
            missing_data=missing_lines,
            freshness_summary=freshness_summary,
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="candidate_universe",
                    payload_path="$.candidates",
                )
            ],
        )
