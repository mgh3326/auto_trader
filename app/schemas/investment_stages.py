"""Pydantic schemas for investment stage runs/artifacts (ROB-279)."""

from __future__ import annotations

import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

StageTypeLiteral = (
    "market",
    "news",
    "portfolio_journal",
    "watch_context",
    "candidate_universe",
    "bull_reducer",
    "bear_reducer",
    "risk_review",
)


class StageVerdict(enum.StrEnum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class StageCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_uuid: uuid.UUID
    snapshot_kind: str
    payload_path: str | None = None


class StageArtifactPayload(BaseModel):
    """Structured output that every stage MUST emit."""

    model_config = ConfigDict(extra="forbid")

    stage_type: str
    verdict: StageVerdict
    confidence: int = Field(ge=0, le=100)
    summary: str | None = None
    key_points: list[str] = Field(default_factory=list)
    buy_evidence: list[str] = Field(default_factory=list)
    sell_evidence: list[str] = Field(default_factory=list)
    risk_evidence: list[str] = Field(default_factory=list)
    missing_data: list[str] = Field(default_factory=list)
    cited_snapshots: list[StageCitation] = Field(default_factory=list)
    freshness_summary: dict[str, Any] | None = None
    model_name: str | None = None
    prompt_version: str | None = None


class StageRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    run_uuid: uuid.UUID
    snapshot_bundle_uuid: uuid.UUID
    market: str
    market_session: str | None
    account_scope: str | None
    policy_version: str
    generator_version: str
    status: str
    started_at: str
    completed_at: str | None
    metadata_json: dict[str, Any] | None = None


class StageArtifactRead(StageArtifactPayload):
    artifact_uuid: uuid.UUID
    run_uuid: uuid.UUID
    created_at: str
