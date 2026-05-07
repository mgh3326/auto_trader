"""Pydantic response schemas for market events (ROB-128)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MarketEventValueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric_name: str
    period: str | None = None
    actual: Decimal | None = None
    forecast: Decimal | None = None
    previous: Decimal | None = None
    revised_previous: Decimal | None = None
    unit: str | None = None
    surprise: Decimal | None = None
    surprise_pct: Decimal | None = None
    released_at: datetime | None = None


class MarketEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    market: str
    country: str | None = None
    currency: str | None = None
    symbol: str | None = None
    company_name: str | None = None
    title: str | None = None
    event_date: date
    release_time_utc: datetime | None = None
    time_hint: str | None = None
    importance: int | None = None
    status: str = "scheduled"
    source: str
    source_event_id: str | None = None
    source_url: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None

    held: bool | None = None
    watched: bool | None = None

    values: list[MarketEventValueResponse] = Field(default_factory=list)


class MarketEventsDayResponse(BaseModel):
    date: date
    events: list[MarketEventResponse] = Field(default_factory=list)


class MarketEventsRangeResponse(BaseModel):
    from_date: date
    to_date: date
    count: int
    events: list[MarketEventResponse] = Field(default_factory=list)


class IngestionPartitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    category: str
    market: str
    partition_date: date
    status: str
    event_count: int
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    retry_count: int


class IngestionRunResult(BaseModel):
    source: str
    category: str
    market: str
    partition_date: date
    status: str
    event_count: int
    error: str | None = None
