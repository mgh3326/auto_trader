"""Freshness diagnostics schemas for market events ingestion (ROB-208)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class MarketEventsFreshnessRow(BaseModel):
    """One freshness/coverage row for a market-events source/category/market key."""

    source: str
    category: str
    market: str
    window_from: date
    window_to: date
    partition_count_total: int
    partition_count_succeeded: int
    partition_count_failed: int
    partition_count_running: int
    partition_count_pending: int
    partition_count_missing: int
    event_count_in_window: int
    latest_succeeded_partition_date: date | None
    latest_succeeded_finished_at: datetime | None
    hours_since_latest_succeeded: float | None
    latest_failed_partition_date: date | None
    latest_failed_error: str | None
    expected_next_refresh_at: datetime | None
    stale: bool


class MarketEventsFreshnessResponse(BaseModel):
    """Read-only market-events freshness response."""

    generated_at: datetime
    window_from: date
    window_to: date
    stale_threshold_hours: float
    rows: list[MarketEventsFreshnessRow] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
