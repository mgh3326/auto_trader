"""Pydantic response schemas for Discover calendar (ROB-138)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class DiscoverCalendarEvent(BaseModel):
    title: str
    badge: str | None = None
    category: str
    market: str
    symbol: str | None = None
    subtitle: str | None = None
    time_label: str | None = None
    priority: str
    source_event_id: str | None = None


class DiscoverCalendarDay(BaseModel):
    date: date
    weekday: str
    is_today: bool
    events: list[DiscoverCalendarEvent] = Field(default_factory=list)
    hidden_count: int = 0


class DiscoverCalendarResponse(BaseModel):
    headline: str | None = None
    week_label: str
    from_date: date
    to_date: date
    today: date
    tab: str
    days: list[DiscoverCalendarDay] = Field(default_factory=list)
