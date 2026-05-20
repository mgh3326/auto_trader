"""Deterministic watch_context stage (ROB-279)."""

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


class WatchContextStage:
    stage_type = "watch_context"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("watch_context")
        if not snapshots:
            raise UnavailableStageError("watch_context snapshot missing — required")

        snap = snapshots[0]
        payload = snap.payload_json or {}
        active = payload.get("active_alerts", [])

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=StageVerdict.NEUTRAL,
            confidence=50 if active else 30,
            summary=f"{len(active)} active watch alerts",
            key_points=[
                f"{a.get('symbol', '?')}: {a.get('condition', '?')}" for a in active[:5]
            ],
            cited_snapshots=[
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="watch_context",
                    payload_path="$.active_alerts",
                )
            ],
        )
