"""Hermes dimension-report ingest schemas (ROB-306).

Push-only (Hermes pulls context, writes the per-dimension analyst report
out-of-process, PUSHES here). auto_trader validates + persists; never calls an
LLM in-process. Vocab tuples come from the ORM model (single source of truth).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.investment_dimension_reports import DIMENSIONS, MARKETS, STANCES
from app.schemas.hermes_composition import HermesStageRunEnvelope

HERMES_DIMENSION_REPORTS_VERSION = "hermes-dimension-reports.v1"
MAX_DIMENSION_REPORTS_PER_CALL = 50


class HermesDimensionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    market: str = "us"
    symbol: str | None = None  # null = market-wide
    report_text: str | None = None
    key_findings: list[Any] | None = None
    signals: dict[str, Any] | None = None
    stance: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    missing_data: list[Any] | None = None
    freshness_summary: dict[str, Any] | None = None
    cited_snapshot_uuids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("dimension")
    @classmethod
    def _dim_in_vocab(cls, v: str) -> str:
        if v not in DIMENSIONS:
            raise ValueError(f"dimension={v!r} not in {DIMENSIONS!r}")
        return v

    @field_validator("market")
    @classmethod
    def _market_in_vocab(cls, v: str) -> str:
        if v not in MARKETS:
            raise ValueError(f"market={v!r} not in {MARKETS!r}")
        return v

    @field_validator("stance")
    @classmethod
    def _stance_in_vocab(cls, v: str | None) -> str | None:
        if v is not None and v not in STANCES:
            raise ValueError(f"stance={v!r} not in {STANCES!r}")
        return v


class HermesDimensionReportsIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_version: Literal["hermes-dimension-reports.v1"] = (
        HERMES_DIMENSION_REPORTS_VERSION
    )
    run_envelope: HermesStageRunEnvelope
    dimension_reports: list[HermesDimensionReport] = Field(
        min_length=1, max_length=MAX_DIMENSION_REPORTS_PER_CALL
    )
