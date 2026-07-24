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
    "briefing",
]
AnalysisArtifactCreatedByLiteral = Literal["claude", "operator", "system", "codex"]
# ROB-648 reduced advisory readiness enum. tradingcodex-lane labels are
# excluded; server-derived readiness gating is deferred (needs per-kind payload
# schemas, P5+). Caller declares this; it is not a hard short-circuit gate.
AnalysisArtifactReadinessLiteral = Literal[
    "screen_grade",
    "not_decision_ready",
    "ready_for_order_review",
    "blocked",
]


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
    correlation_id: str | None = None
    account_scope: str | None = None
    # Advisory only. content_hash/version are server-computed and intentionally
    # NOT accepted from the caller.
    readiness_label: AnalysisArtifactReadinessLiteral | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("as_of", "valid_until", mode="before")
    @classmethod
    def _coerce_datetime_to_kst(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                from dateutil.parser import parse

                value = parse(value)
            except Exception:
                pass
        if isinstance(value, datetime):
            if value.tzinfo is None:
                from app.core.timezone import KST

                value = value.replace(tzinfo=KST)
        return value

    @field_validator("payload", mode="after")
    @classmethod
    def _validate_payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        # ensure_ascii=False: measure real UTF-8 bytes, not the ~6x escaped
        # size for Korean text (ROB-628 lesson). 100 KB cap.
        payload_json = json.dumps(value, ensure_ascii=False, default=str)
        if len(payload_json.encode("utf-8")) > 100 * 1024:
            raise ValueError("payload size must not exceed 100KB")
        return value

    @field_validator("title", "session_label", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("symbols", mode="before")
    @classmethod
    def _clean_symbols(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("symbols must be a list")
        from app.core.symbol import to_db_symbol

        cleaned = [
            to_db_symbol(str(item).strip()) for item in value if str(item).strip()
        ]
        return cleaned


class AnalysisArtifactListRequest(BaseModel):
    """Filter parameters for listing analysis artifacts."""

    market: MarketLiteral | None = None
    kind: AnalysisArtifactKindLiteral | None = None
    symbol: str | None = None
    since: datetime | None = None
    include_stale: bool = False
    limit: int = Field(default=20, ge=1)
    correlation_id: str | None = None
    account_scope: str | None = None
    readiness_label: AnalysisArtifactReadinessLiteral | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("since", mode="before")
    @classmethod
    def _coerce_since_to_kst(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                from dateutil.parser import parse

                value = parse(value)
            except Exception:
                pass
        if isinstance(value, datetime):
            if value.tzinfo is None:
                from app.core.timezone import KST

                value = value.replace(tzinfo=KST)
        return value

    @field_validator("symbol", mode="before")
    @classmethod
    def _strip_symbol(cls, value: object) -> object:
        if isinstance(value, str):
            val = value.strip()
            if val:
                from app.core.symbol import to_db_symbol

                return to_db_symbol(val)
            return None
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
    correlation_id: str | None
    account_scope: str | None
    content_hash: str | None
    version: int
    readiness_label: AnalysisArtifactReadinessLiteral | None
    is_stale: bool
    created_by: AnalysisArtifactCreatedByLiteral
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AnalysisArtifactRead(AnalysisArtifactMeta):
    """Full artifact including the payload."""

    payload: dict[str, Any]
    payload_size_bytes: int


class AnalysisArtifactSaveResponse(BaseModel):
    success: Literal[True] = True
    # 'unchanged': an exact correlation_id retry whose payload and persisted
    # metadata are identical — no write, version preserved (ROB-1048).
    action: Literal["created", "updated", "unchanged"] = "created"
    artifact: AnalysisArtifactRead


class AnalysisArtifactListResponse(BaseModel):
    success: Literal[True] = True
    count: int
    filters: AnalysisArtifactListRequest
    artifacts: list[AnalysisArtifactMeta]


class AnalysisArtifactGetResponse(BaseModel):
    success: Literal[True] = True
    artifact: AnalysisArtifactRead
