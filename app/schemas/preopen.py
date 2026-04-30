"""Pydantic schemas for the preopen dashboard endpoint (ROB-39)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

NewsReadinessStatus = Literal["ready", "stale", "unavailable"]


class NewsArticlePreview(BaseModel):
    id: int
    title: str
    url: str
    source: str | None
    feed_source: str | None
    published_at: datetime | None
    summary: str | None


class NewsReadinessSummary(BaseModel):
    status: NewsReadinessStatus
    is_ready: bool
    is_stale: bool
    latest_run_uuid: str | None
    latest_status: str | None
    latest_finished_at: datetime | None
    latest_article_published_at: datetime | None
    source_counts: dict[str, int]
    warnings: list[str]
    max_age_minutes: int


class CandidateSummary(BaseModel):
    candidate_uuid: UUID
    symbol: str
    instrument_type: str
    side: Literal["buy", "sell", "none"]
    candidate_kind: str
    proposed_price: Decimal | None
    proposed_qty: Decimal | None
    confidence: int | None
    rationale: str | None
    currency: str | None
    warnings: list[str]


class ReconciliationSummary(BaseModel):
    order_id: str
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    classification: str
    nxt_classification: str | None
    nxt_actionable: bool | None
    gap_pct: Decimal | None
    summary: str | None
    reasons: list[str]
    warnings: list[str]


class LinkedSessionRef(BaseModel):
    session_uuid: UUID
    status: str
    created_at: datetime


class PreopenLatestResponse(BaseModel):
    has_run: bool
    advisory_used: bool = False
    advisory_skipped_reason: str | None = None
    run_uuid: UUID | None
    market_scope: Literal["kr", "us", "crypto"] | None
    stage: Literal["preopen"] | None
    status: str | None
    strategy_name: str | None
    source_profile: str | None
    generated_at: datetime | None
    created_at: datetime | None
    notes: str | None
    market_brief: dict[str, Any] | None
    source_freshness: dict[str, Any] | None
    source_warnings: list[str]
    advisory_links: list[dict[str, Any]]
    candidate_count: int
    reconciliation_count: int
    candidates: list[CandidateSummary]
    reconciliations: list[ReconciliationSummary]
    linked_sessions: list[LinkedSessionRef]
    news: NewsReadinessSummary | None = None
    news_preview: list[NewsArticlePreview] = []
