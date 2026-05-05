# app/schemas/news_radar.py
"""Schemas for the Market Risk News Radar read-only contract (ROB-109)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

NewsRadarReadinessStatus = Literal["ready", "stale", "unavailable"]
NewsRadarSeverity = Literal["high", "medium", "low"]
NewsRadarRiskCategory = Literal[
    "geopolitical_oil",
    "macro_policy",
    "crypto_security",
    "earnings_bigtech",
    "korea_market",
]
NewsRadarMarket = Literal["all", "kr", "us", "crypto"]


class NewsRadarReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: NewsRadarReadinessStatus
    latest_scraped_at: datetime | None
    latest_published_at: datetime | None
    recent_6h_count: int
    recent_24h_count: int
    source_count: int
    stale: bool
    max_age_minutes: int
    warnings: list[str] = Field(default_factory=list)


class NewsRadarSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    high_risk_count: int
    total_count: int
    included_in_briefing_count: int
    excluded_but_collected_count: int


class NewsRadarSourceCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed_source: str
    recent_6h: int = 0
    recent_24h: int = 0
    latest_published_at: datetime | None = None
    latest_scraped_at: datetime | None = None
    status: str = "unavailable"


class NewsRadarItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    source: str | None
    feed_source: str | None
    url: str
    published_at: datetime | None
    market: str
    risk_category: NewsRadarRiskCategory | None
    severity: NewsRadarSeverity
    themes: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    included_in_briefing: bool
    briefing_reason: str | None
    briefing_score: int
    snippet: str | None
    matched_terms: list[str] = Field(default_factory=list)


class NewsRadarSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: NewsRadarRiskCategory
    title: str
    severity: NewsRadarSeverity
    items: list[NewsRadarItem] = Field(default_factory=list)


class NewsRadarResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: NewsRadarMarket
    as_of: datetime
    readiness: NewsRadarReadiness
    summary: NewsRadarSummary
    sections: list[NewsRadarSection] = Field(default_factory=list)
    items: list[NewsRadarItem] = Field(default_factory=list)
    excluded_items: list[NewsRadarItem] = Field(default_factory=list)
    source_coverage: list[NewsRadarSourceCoverage] = Field(default_factory=list)
