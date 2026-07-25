from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.investment_snapshots import SnapshotAccountScope, SnapshotMarket

AnalysisSectionName = Literal[
    "portfolio",
    "quotes_orderbooks",
    "indicators_support_resistance",
    "market_gate_inputs",
    "investor_flow",
    "decision_history",
]
ANALYSIS_SECTION_NAMES: tuple[AnalysisSectionName, ...] = (
    "portfolio",
    "quotes_orderbooks",
    "indicators_support_resistance",
    "market_gate_inputs",
    "investor_flow",
    "decision_history",
)


class AnalysisBundleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbols: list[str] = Field(min_length=1, max_length=10)
    user_id: int | None = None
    market_session: str | None = None
    requested_by: Literal["user", "claude_code", "reviewer"] = "claude_code"


class AnalysisSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "partial", "unavailable"]
    collected_at: dt.datetime
    as_of: dt.datetime
    source: dict[str, Any]
    soft_ttl_seconds: int = Field(gt=0)
    hard_ttl_seconds: int = Field(gt=0)
    data: Any | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_payload_or_error(self) -> Self:
        if self.status == "unavailable" and not self.error:
            raise ValueError("unavailable section requires error")
        if self.status != "unavailable" and self.data is None:
            raise ValueError("available section requires data")
        if self.hard_ttl_seconds < self.soft_ttl_seconds:
            raise ValueError("hard TTL must be >= soft TTL")
        return self


class AnalysisFrozenDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["analysis-snapshot-bundle.v1"] = (
        "analysis-snapshot-bundle.v1"
    )
    captured_at: dt.datetime
    request: AnalysisBundleCreateRequest
    sections: dict[AnalysisSectionName, AnalysisSection]

    @model_validator(mode="after")
    def require_all_sections(self) -> Self:
        if set(self.sections) != set(ANALYSIS_SECTION_NAMES):
            raise ValueError("frozen document must contain every analysis section")
        return self


class AnalysisBundleCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: uuid.UUID
    content_hash: str = Field(min_length=64, max_length=64)
    status: Literal["complete", "partial"]
    captured_at: dt.datetime
    unavailable_sections: list[AnalysisSectionName]
    partial_sections: list[AnalysisSectionName]


class AnalysisSectionFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: dt.datetime
    age_seconds: float = Field(ge=0)
    status: Literal["fresh", "soft_stale", "hard_stale"]
    source: dict[str, Any]
    capture_status: Literal["ok", "partial", "unavailable"]


class AnalysisBundleGetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_id: uuid.UUID
    content_hash: str
    integrity_verified: Literal[True] = True
    created_at: dt.datetime
    captured_at: dt.datetime
    read_at: dt.datetime
    age_seconds: float = Field(ge=0)
    status: Literal["complete", "partial"]
    completeness: dict[str, list[str]]
    stale_warning: bool
    section_freshness: dict[str, AnalysisSectionFreshness]
    document: dict[str, Any]
