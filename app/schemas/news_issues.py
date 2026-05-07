"""Pydantic schemas for the market issue clustering read-only API (ROB-130)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketIssueMarket = Literal["kr", "us", "crypto"]
IssueDirection = Literal["up", "down", "mixed", "neutral"]


class IssueSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recency_score: float = Field(ge=0.0, le=1.0)
    source_diversity_score: float = Field(ge=0.0, le=1.0)
    mention_score: float = Field(ge=0.0, le=1.0)


class MarketIssueArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    url: str
    source: str | None
    feed_source: str | None
    published_at: datetime | None
    summary: str | None = None
    matched_terms: list[str] = Field(default_factory=list)


class MarketIssueRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    canonical_name: str
    mention_count: int = 0


class MarketIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    market: MarketIssueMarket
    rank: int
    issue_title: str
    subtitle: str | None
    direction: IssueDirection
    source_count: int
    article_count: int
    updated_at: datetime
    summary: str | None = None
    related_symbols: list[MarketIssueRelatedSymbol] = Field(default_factory=list)
    related_sectors: list[str] = Field(default_factory=list)
    articles: list[MarketIssueArticle] = Field(default_factory=list)
    signals: IssueSignals


class MarketIssuesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketIssueMarket | Literal["all"]
    as_of: datetime
    window_hours: int
    items: list[MarketIssue] = Field(default_factory=list)
