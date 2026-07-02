"""ROB-637 analysis artifact DTOs for MCP and service boundaries."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.investment_reports import MarketLiteral

AnalysisArtifactKindLiteral = Literal[
    "screening_ranking",
    "profit_taking_verdicts",
    "support_resistance_map",
    "flow_assessment",
    "candidate_pool",
    "session_summary",
]
AnalysisArtifactCreatedByLiteral = Literal["claude", "operator", "system"]


class AnalysisArtifactSave(BaseModel):
    """Input payload for persisting a single analysis artifact."""

    market: MarketLiteral
    kind: AnalysisArtifactKindLiteral
    title: str = Field(min_length=1)
    symbols: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    as_of: datetime
    valid_until: datetime | None = None
    created_by: AnalysisArtifactCreatedByLiteral = "claude"
    session_label: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("title", "session_label", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("symbols", mode="before")
    @classmethod
    def _clean_symbols(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("symbols must be a list")
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned


class AnalysisArtifactListRequest(BaseModel):
    """Filter parameters for listing analysis artifacts."""

    market: MarketLiteral | None = None
    kind: AnalysisArtifactKindLiteral | None = None
    symbol: str | None = None
    since: datetime | None = None
    include_stale: bool = False
    limit: int = Field(default=20, ge=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("symbol", mode="before")
    @classmethod
    def _strip_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp_limit(cls, value: object) -> int:
        limit = 20 if value is None else int(value)
        return max(1, min(limit, 100))


class AnalysisArtifactMeta(BaseModel):
    """Lightweight artifact metadata (no payload) for list responses."""

    id: int
    artifact_uuid: UUID
    market: MarketLiteral
    kind: AnalysisArtifactKindLiteral
    title: str
    symbols: list[str]
    as_of: datetime
    valid_until: datetime | None
    session_label: str | None
    created_by: AnalysisArtifactCreatedByLiteral
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AnalysisArtifactRead(AnalysisArtifactMeta):
    """Full artifact including the payload."""

    payload: dict[str, Any]


class AnalysisArtifactSaveResponse(BaseModel):
    success: Literal[True] = True
    artifact: AnalysisArtifactRead


class AnalysisArtifactListResponse(BaseModel):
    success: Literal[True] = True
    count: int
    filters: AnalysisArtifactListRequest
    artifacts: list[AnalysisArtifactMeta]


class AnalysisArtifactGetResponse(BaseModel):
    success: Literal[True] = True
    artifact: AnalysisArtifactRead
