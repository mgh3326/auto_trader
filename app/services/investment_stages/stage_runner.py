"""Stage runner orchestrator (ROB-279)."""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageRun
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_snapshots.read_service import SnapshotBundleReadService
from app.services.investment_stages.repository import InvestmentStagesRepository
from app.services.investment_stages.stages.base import (
    Stage,
    StageContext,
    UnavailableStageError,
)

_logger = logging.getLogger(__name__)


class StageRunner:
    def __init__(
        self,
        *,
        session: AsyncSession,
        bundle_read_service: SnapshotBundleReadService | object,
        stages: Iterable[Stage],
    ) -> None:
        self._session = session
        self._bundle_read = bundle_read_service
        self._stages = list(stages)
        self._repo = InvestmentStagesRepository(session)

    async def run(
        self,
        *,
        snapshot_bundle_uuid: uuid.UUID,
        market: str,
        market_session: str | None,
        account_scope: str | None,
        policy_version: str = "v1",
        generator_version: str = "v1",
    ) -> InvestmentStageRun:
        run = await self._repo.create_run(
            snapshot_bundle_uuid=snapshot_bundle_uuid,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            policy_version=policy_version,
            generator_version=generator_version,
        )

        bundle_response = await self._bundle_read.get_bundle(
            bundle_uuid=snapshot_bundle_uuid
        )
        snapshots_by_kind: dict[str, list] = defaultdict(list)
        for item in getattr(bundle_response, "items", []):
            snapshot = getattr(item, "snapshot", None) or item
            kind = getattr(snapshot, "snapshot_kind", None)
            if kind:
                snapshots_by_kind[kind].append(snapshot)

        bundle = getattr(bundle_response, "bundle", None)
        ctx = StageContext(
            bundle_uuid=snapshot_bundle_uuid,
            snapshots_by_kind=dict(snapshots_by_kind),
            bundle_metadata={
                "status": getattr(bundle, "status", None),
                "freshness_summary": getattr(bundle, "freshness_summary", None),
            },
            market=market,
            prior_artifacts={},
        )

        for stage in self._stages:
            try:
                payload = await stage.run(ctx)
            except UnavailableStageError as exc:
                _logger.info("stage %s unavailable: %s", stage.stage_type, exc)
                payload = StageArtifactPayload(
                    stage_type=stage.stage_type,
                    verdict=StageVerdict.UNAVAILABLE,
                    confidence=0,
                    summary=str(exc),
                    missing_data=[stage.stage_type],
                )
            except Exception as exc:  # noqa: BLE001
                _logger.exception("stage %s failed", stage.stage_type)
                payload = StageArtifactPayload(
                    stage_type=stage.stage_type,
                    verdict=StageVerdict.UNAVAILABLE,
                    confidence=0,
                    summary=f"stage error: {exc!r}",
                    missing_data=[stage.stage_type],
                )
            await self._repo.persist_artifact(run.run_uuid, payload)
            ctx.prior_artifacts[stage.stage_type] = payload

        await self._repo.complete_run(run.run_uuid, status="completed")
        await self._session.refresh(run)
        return run
