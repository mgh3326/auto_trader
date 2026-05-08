"""ROB-142 — /invest/api/feed/news schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.news_issues import MarketIssue

FeedTab = Literal["top", "latest", "hot", "holdings", "watchlist", "kr", "us", "crypto"]
NewsMarket = Literal["kr", "us", "crypto"]
RelationKind = Literal["held", "watchlist", "both", "none"]


class NewsRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: NewsMarket
    displayName: str


class FeedNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    publisher: str | None = None
    feedSource: str | None = None
    publishedAt: datetime | None = None
    market: NewsMarket
    relatedSymbols: list[NewsRelatedSymbol] = Field(default_factory=list)
    issueId: str | None = None
    summarySnippet: str | None = None
    relation: RelationKind = "none"
    url: str


class FeedNewsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FeedNewsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: FeedTab
    asOf: datetime
    issues: list[MarketIssue] = Field(default_factory=list)
    items: list[FeedNewsItem] = Field(default_factory=list)
    nextCursor: str | None = None
    meta: FeedNewsMeta = Field(default_factory=FeedNewsMeta)
