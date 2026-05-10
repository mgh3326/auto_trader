"""ROB-142 — /invest/api/feed/news schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.news_issues import MarketIssue

FeedTab = Literal["top", "latest", "hot", "holdings", "watchlist", "kr", "us", "crypto"]
NewsMarket = Literal["kr", "us", "crypto"]
RelationKind = Literal["held", "watchlist", "both", "none"]
# ROB-155: article scope — market_wide means broad macro/index/sector article;
# symbol_specific means article thesis anchors on one or more specific symbols;
# mixed means both a broad frame and a clearly anchored specific symbol.
# ROB-169: kr_market_wide is the KR analogue of market_wide for KOSPI/KOSDAQ/
# 금리/환율/반도체/정책 articles that lack a stock_symbol but are investment-relevant.
NewsScope = Literal["market_wide", "symbol_specific", "mixed", "kr_market_wide"]


class NewsRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: NewsMarket
    displayName: str
    relation: RelationKind = "none"
    matchReason: str | None = None
    matchedTerm: str | None = None
    currentPrice: float | None = None
    previousClose: float | None = None
    change: float | None = None
    changePct: float | None = None
    quoteSource: str | None = None
    quoteAsOf: datetime | None = None


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
    # ROB-155: additive read-layer classification fields; defaults preserve backward compat.
    scope: NewsScope = "symbol_specific"
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    noiseReason: str | None = None


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
