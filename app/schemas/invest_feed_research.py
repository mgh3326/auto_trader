"""ROB-179 — /invest/api/feed/research schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.research_reports import ResearchReportSymbolCandidate

FeedResearchTab = Literal["top", "latest", "mine", "watchlist", "holdings", "kr", "us"]
ResearchRelation = Literal["mine", "watch", "none"]
ResearchMarket = Literal["kr", "us", "crypto"]


class FeedResearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: int
    source: str
    title: str | None = None
    analyst: str | None = None
    publishedAtText: str | None = Field(
        default=None, validation_alias="published_at_text"
    )
    publishedAt: datetime | None = Field(default=None, validation_alias="published_at")
    category: str | None = None
    detailUrl: str | None = Field(default=None, validation_alias="detail_url")
    pdfUrl: str | None = Field(default=None, validation_alias="pdf_url")
    excerpt: str | None = None
    symbolCandidates: list[ResearchReportSymbolCandidate] = Field(
        default_factory=list, validation_alias="symbol_candidates"
    )
    attributionPublisher: str | None = Field(
        default=None, validation_alias="attribution_publisher"
    )
    attributionCopyrightNotice: str | None = Field(
        default=None, validation_alias="attribution_copyright_notice"
    )
    market: ResearchMarket | None = None
    relation: ResearchRelation = "none"


class FeedResearchAppliedFilters(BaseModel):
    source: str | None = None
    symbol: str | None = None
    analyst: str | None = None
    category: str | None = None
    query: str | None = None
    fromDate: date | None = None
    toDate: date | None = None


class FeedResearchMeta(BaseModel):
    limit: int
    appliedFilters: FeedResearchAppliedFilters


class FeedResearchResponse(BaseModel):
    tab: FeedResearchTab
    asOf: datetime
    items: list[FeedResearchItem]
    nextCursor: str | None
    meta: FeedResearchMeta


@dataclass
class FeedResearchFilters:
    """Router-to-service filter bag."""

    source: str | None = None
    symbol: str | None = None
    analyst: str | None = None
    category: str | None = None
    query: str | None = None
    from_date: date | None = None
    to_date: date | None = None
