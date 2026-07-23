"""Read-only schemas for /invest forecast calibration surface (ROB-663)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.trading import InstrumentType

# The calibration cohort keys accepted by ``build_forecast_calibration_aggregate``
# (mirrors ``forecast_service._GROUP_BY_FIELDS``). "day" is the KST calendar date.
VALID_GROUP_BY = frozenset({"created_by", "session_label", "model_label", "day"})
VALID_INSTRUMENT_TYPES = frozenset(t.value for t in InstrumentType)

ForecastKind = Literal["open", "closed"]


class CalibrationGroupRow(BaseModel):
    # extra=ignore: fed the full aggregate group dict; we keep a stable subset.
    model_config = ConfigDict(extra="ignore")

    group: str
    sample_size: int = Field(ge=0)
    hits: int = Field(ge=0)
    misses: int = Field(ge=0)
    hit_rate: float | None = None
    avg_brier_score: float | None = None
    avg_probability: float | None = None
    calibration_gap: float | None = None


class CalibrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_by: str
    created_by: str | None = None
    symbol: str | None = None
    instrument_type: str | None = None
    days: int | None = None
    count: int = Field(ge=0)
    groups: list[CalibrationGroupRow]
    as_of: datetime


class ForecastRow(BaseModel):
    # extra=ignore: fed the full serialize_forecast(...) dict; we keep a subset.
    model_config = ConfigDict(extra="ignore")

    id: int
    forecast_id: str
    correlation_id: str | None = None
    created_by: str | None = None
    session_label: str | None = None
    model_label: str | None = None
    symbol: str
    instrument_type: str | None = None
    forecast_target: dict[str, Any] | None = None
    immutable_claim: dict[str, Any] | None = None
    immutable_claim_hash: str | None = None
    target_version: int = Field(default=0, ge=0)
    resolution_semantics_status: str | None = None
    semantics_evidence: dict[str, Any] | None = None
    supersedes_forecast_id: str | None = None
    superseded_by_forecast_id: str | None = None
    horizon: str | None = None
    probability: float | None = None
    probability_range_low: float | None = None
    probability_range_high: float | None = None
    resolution_source: str | None = None
    review_date: str | None = None
    status: str | None = None
    outcome: bool | None = None
    observed_value: float | None = None
    resolved_at: str | None = None
    brier_score: float | None = None
    resolution_detail: dict[str, Any] | None = None
    created_at: str | None = None


class ForecastListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ForecastKind
    symbol: str | None = None
    created_by: str | None = None
    instrument_type: str | None = None
    count: int = Field(ge=0)
    items: list[ForecastRow]
    as_of: datetime
