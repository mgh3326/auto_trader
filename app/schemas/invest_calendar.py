"""ROB-144 — /invest/api/calendar + weekly-summary schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarDayState,
    CalendarSourceStatus,
)

CalendarMarket = Literal["kr", "us", "crypto", "global"]
EventType = Literal["earnings", "economic", "disclosure", "crypto", "other"]
RelationKind = Literal["held", "watchlist", "both", "none"]
Badge = Literal["holdings", "watchlist", "major"]
CalendarTab = Literal["all", "economic", "earnings", "disclosure", "crypto"]
HighlightReason = Literal[
    "held",
    "watchlist",
    "major",
    "high_impact",
    "near_term",
    "has_values",
]
ImpactTag = Literal["fx", "rates", "inflation", "jobs", "central_bank"]


class CalendarRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: Literal["kr", "us", "crypto"]
    displayName: str


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    eventId: str
    title: str
    market: CalendarMarket
    eventType: EventType
    eventTimeLocal: str | None = None
    source: str
    country: str | None = None
    currency: str | None = None
    importance: int | None = None
    impactTags: list[ImpactTag] = Field(default_factory=list)
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    relatedSymbols: list[CalendarRelatedSymbol] = Field(default_factory=list)
    relation: RelationKind = "none"
    badges: list[Badge] = Field(default_factory=list)
    displayPriority: int = 0
    highlightReasons: list[HighlightReason] = Field(default_factory=list)


class CalendarCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clusterId: str
    label: str
    eventType: EventType
    market: CalendarMarket
    eventCount: int
    topEvents: list[CalendarEvent] = Field(default_factory=list)


class CalendarDaySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: str | None = None
    highlightEventIds: list[str] = Field(default_factory=list)
    overflowCount: int = 0
    overflowLabel: str | None = None


class CalendarDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    events: list[CalendarEvent] = Field(default_factory=list)
    clusters: list[CalendarCluster] = Field(default_factory=list)
    dataState: CalendarDayState = "loaded"
    summary: CalendarDaySummary | None = None


class CalendarMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[str] = Field(default_factory=list)
    sourceFreshness: list[CalendarSourceStatus] = Field(default_factory=list)
    coverage: CalendarCoverage | None = None


class CalendarResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: CalendarTab
    fromDate: date
    toDate: date
    asOf: datetime
    days: list[CalendarDay] = Field(default_factory=list)
    meta: CalendarMeta = Field(default_factory=CalendarMeta)


class WeeklySection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    reportType: str
    market: str | None = None
    title: str
    body: str


class WeeklySummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekStart: date
    asOf: datetime
    sections: list[WeeklySection] = Field(default_factory=list)
    partial: bool = False
    missingDates: list[date] = Field(default_factory=list)
