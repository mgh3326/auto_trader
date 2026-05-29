"""Stage protocol and shared context (ROB-279)."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol

from app.models.investment_snapshots import InvestmentSnapshot
from app.schemas.investment_stages import StageArtifactPayload


class UnavailableStageError(Exception):
    """Raised by a stage when required snapshots are absent.
    The runner converts this to an `UNAVAILABLE` artifact rather than failing the run."""


@dataclasses.dataclass(frozen=True)
class StageContext:
    bundle_uuid: uuid.UUID
    snapshots_by_kind: dict[str, list[InvestmentSnapshot]]
    bundle_metadata: dict[str, Any]
    # Bundle market ("kr" / "us" / "crypto"). Lets a stage branch on market
    # (e.g. MarketStage selecting KOSPI vs SPX). Defaults to None so callers
    # that pre-date the field still construct a valid context.
    market: str | None = None
    prior_artifacts: dict[str, StageArtifactPayload] = dataclasses.field(
        default_factory=dict
    )

    def snapshots_for(self, kind: str) -> list[InvestmentSnapshot]:
        return self.snapshots_by_kind.get(kind, [])

    def artifact_for(self, stage_type: str) -> StageArtifactPayload | None:
        return self.prior_artifacts.get(stage_type)


class Stage(Protocol):
    stage_type: str

    async def run(self, context: StageContext) -> StageArtifactPayload: ...
