"""Read-only freshness/coverage schemas for /invest/calendar diagnostics (ROB-167)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Per-day data state, derived from the expected partitions for that date:
#  - "loaded"       : every expected partition succeeded with event_count >= 0
#  - "empty"        : every expected partition succeeded but event_count == 0
#  - "partial"      : some expected partitions succeeded, some missing/failed
#  - "missing"      : zero partitions exist for this date (never ingested)
#  - "error"        : at least one expected partition is in failed state
#  - "stale"        : every expected partition succeeded but the most recent
#                     finished_at is older than the configured staleness window
CalendarDayState = Literal["loaded", "empty", "partial", "missing", "error", "stale"]

# Per-source aggregate freshness state across the requested range.
CalendarSourceState = Literal["fresh", "stale", "failed", "missing"]


class CalendarSourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    category: str
    market: str
    state: CalendarSourceState
    lastSuccessAt: datetime | None = None
    lastFailureAt: datetime | None = None
    lastError: str | None = None
    succeededPartitions: int = 0
    failedPartitions: int = 0
    missingPartitions: int = 0
    eventCount: int = 0


class CalendarCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromDate: date
    toDate: date
    expectedPartitions: int
    succeededPartitions: int
    failedPartitions: int
    missingPartitions: int
    totalEvents: int


class CoveragePartitionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    category: str
    market: str
    partitionDate: date
    status: Literal[
        "expected_missing", "pending", "running", "succeeded", "failed", "partial"
    ]
    eventCount: int = 0
    startedAt: datetime | None = None
    finishedAt: datetime | None = None
    lastError: str | None = None
    retryCount: int = 0


class CoverageMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fromDate: date
    toDate: date
    asOf: datetime
    sources: list[CalendarSourceStatus] = Field(default_factory=list)
    partitions: list[CoveragePartitionRow] = Field(default_factory=list)
    coverage: CalendarCoverage
