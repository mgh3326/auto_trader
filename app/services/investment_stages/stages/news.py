"""Deterministic news stage (ROB-279)."""

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


class NewsStage:
    stage_type = "news"

    async def run(self, context: StageContext) -> StageArtifactPayload:
        snapshots = context.snapshots_for("news")
        if not snapshots:
            raise UnavailableStageError("news snapshot missing")

        articles: list[dict] = []
        citations: list[StageCitation] = []
        for snap in snapshots:
            payload = snap.payload_json or {}
            for art in payload.get("articles", []):
                articles.append(art)
            citations.append(
                StageCitation(
                    snapshot_uuid=snap.snapshot_uuid,
                    snapshot_kind="news",
                    payload_path="$.articles",
                )
            )

        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        total = len(articles)
        if total == 0:
            verdict = StageVerdict.NEUTRAL
            confidence = 10
        elif pos >= neg * 2 and pos >= 3:
            verdict = StageVerdict.BULL
            confidence = min(40 + pos * 5, 80)
        elif neg >= pos * 2 and neg >= 3:
            verdict = StageVerdict.BEAR
            confidence = min(40 + neg * 5, 80)
        else:
            verdict = StageVerdict.NEUTRAL
            confidence = 30

        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=confidence,
            summary=f"{total} articles (pos={pos}, neg={neg})",
            key_points=[a.get("title", "")[:60] for a in articles[:5]],
            cited_snapshots=citations,
            missing_data=[] if articles else ["news_articles"],
        )
