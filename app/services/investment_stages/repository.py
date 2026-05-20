"""Append-only repository for stage runs/artifacts (ROB-279)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageArtifact, InvestmentStageRun
from app.schemas.investment_stages import StageArtifactPayload


class AppendOnlyViolation(Exception):
    """Raised when caller attempts to overwrite an existing stage artifact."""


class InvestmentStagesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(
        self,
        *,
        snapshot_bundle_uuid: uuid.UUID,
        market: str,
        market_session: str | None = None,
        account_scope: str | None = None,
        policy_version: str = "v1",
        generator_version: str = "v1",
    ) -> InvestmentStageRun:
        run = InvestmentStageRun(
            snapshot_bundle_uuid=snapshot_bundle_uuid,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            policy_version=policy_version,
            generator_version=generator_version,
        )
        self._session.add(run)
        await self._session.flush()
        await self._session.refresh(run)
        return run

    async def persist_artifact(
        self, run_uuid: uuid.UUID, payload: StageArtifactPayload
    ) -> InvestmentStageArtifact:
        artifact = InvestmentStageArtifact(
            run_uuid=run_uuid,
            stage_type=payload.stage_type,
            verdict=payload.verdict.value,
            confidence=payload.confidence,
            summary=payload.summary,
            key_points=payload.key_points,
            buy_evidence=payload.buy_evidence,
            sell_evidence=payload.sell_evidence,
            risk_evidence=payload.risk_evidence,
            missing_data=payload.missing_data,
            cited_snapshot_uuids=[c.snapshot_uuid for c in payload.cited_snapshots],
            freshness_summary=payload.freshness_summary,
            model_name=payload.model_name,
            prompt_version=payload.prompt_version,
        )
        self._session.add(artifact)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "ix_investment_stage_artifacts_run_stage" in str(exc):
                raise AppendOnlyViolation(
                    f"stage_type={payload.stage_type} already persisted for run {run_uuid}"
                ) from exc
            raise
        await self._session.refresh(artifact)
        return artifact

    async def complete_run(self, run_uuid: uuid.UUID, *, status: str) -> None:
        run = await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )
        if run is None:
            raise ValueError(f"run not found: {run_uuid}")
        if status not in {"completed", "failed", "blocked"}:
            raise ValueError(f"invalid terminal status: {status}")
        run.status = status
        run.completed_at = dt.datetime.now(tz=dt.UTC)
        await self._session.flush()

    async def get_run(self, run_uuid: uuid.UUID) -> InvestmentStageRun | None:
        return await self._session.scalar(
            select(InvestmentStageRun).where(InvestmentStageRun.run_uuid == run_uuid)
        )

    async def list_artifacts_for_run(
        self, run_uuid: uuid.UUID
    ) -> list[InvestmentStageArtifact]:
        result = await self._session.scalars(
            select(InvestmentStageArtifact)
            .where(InvestmentStageArtifact.run_uuid == run_uuid)
            .order_by(InvestmentStageArtifact.created_at)
        )
        return list(result.all())
