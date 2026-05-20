"""Read-only query service for stage runs (ROB-279)."""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun


@dataclasses.dataclass(frozen=True)
class StageRunWithArtifacts:
    run: InvestmentStageRun
    artifacts: list[InvestmentStageArtifact]


class StageRunQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_run_with_artifacts(
        self, run_uuid: uuid.UUID
    ) -> StageRunWithArtifacts | None:
        run = await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        if run is None:
            return None
        artifacts = list(
            (
                await self._session.scalars(
                    select(InvestmentStageArtifact)
                    .where(InvestmentStageArtifact.run_uuid == run_uuid)
                    .order_by(InvestmentStageArtifact.created_at)
                )
            ).all()
        )
        return StageRunWithArtifacts(run=run, artifacts=artifacts)

    async def list_runs_for_bundle(
        self, snapshot_bundle_uuid: uuid.UUID
    ) -> list[InvestmentStageRun]:
        result = await self._session.scalars(
            select(InvestmentStageRun)
            .where(InvestmentStageRun.snapshot_bundle_uuid == snapshot_bundle_uuid)
            .order_by(InvestmentStageRun.started_at.desc())
        )
        return list(result.all())
