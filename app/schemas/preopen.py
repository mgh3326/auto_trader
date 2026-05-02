"""Pydantic schemas for the preopen dashboard endpoint (ROB-39)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.preopen_news_brief import KRPreopenNewsBrief

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


class PreopenBriefingRelevance(BaseModel):
    score: int
    reason: str
    section_id: str | None = None
    matched_terms: list[str] = []


class PreopenMarketNewsItem(BaseModel):
    id: int
    title: str
    url: str
    source: str | None = None
    feed_source: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    briefing_relevance: PreopenBriefingRelevance | None = None
    crypto_relevance: dict[str, Any] | None = None


class PreopenMarketNewsSection(BaseModel):
    section_id: str
    title: str
    items: list[PreopenMarketNewsItem] = []


class PreopenMarketNewsBriefing(BaseModel):
    briefing_filter: Literal[True] = True
    summary: dict[str, Any]
    sections: list[PreopenMarketNewsSection] = []
    excluded_count: int = 0
    top_excluded: list[PreopenMarketNewsItem] = []


PreopenArtifactStatus = Literal["unavailable", "draft", "ready", "degraded"]
PreopenArtifactReadinessStatus = Literal["ready", "stale", "unavailable", "partial"]
PreopenDecisionSessionCtaState = Literal[
    "unavailable",
    "create_available",
    "linked_session_exists",
]


PreopenQaCheckStatus = Literal["pass", "warn", "fail", "unknown", "skipped"]
PreopenQaCheckSeverity = Literal["info", "low", "medium", "high"]
PreopenQaGrade = Literal["excellent", "good", "watch", "poor", "unavailable"]
PreopenQaConfidence = Literal["high", "medium", "low", "unavailable"]
PreopenQaEvaluatorStatus = Literal["ready", "needs_review", "unavailable", "skipped"]


class PreopenQaCheck(BaseModel):
    id: str
    label: str
    status: PreopenQaCheckStatus
    severity: PreopenQaCheckSeverity
    summary: str
    details: dict[str, Any] | None = None


class PreopenQaScore(BaseModel):
    score: int | None
    grade: PreopenQaGrade
    confidence: PreopenQaConfidence
    reason: str | None = None


class PreopenQaEvaluatorSummary(BaseModel):
    status: PreopenQaEvaluatorStatus
    generated_at: datetime | None = None
    source: Literal["deterministic_v1"] = "deterministic_v1"
    overall: PreopenQaScore
    checks: list[PreopenQaCheck]
    blocking_reasons: list[str]
    warnings: list[str]
    coverage: dict[str, Any]


class PreopenArtifactReadinessItem(BaseModel):
    key: str
    status: PreopenArtifactReadinessStatus
    is_ready: bool
    warnings: list[str] = []
    details: dict[str, Any] = {}


class PreopenArtifactSection(BaseModel):
    section_id: str
    title: str
    item_count: int
    status: PreopenArtifactStatus
    summary: str | None = None
    items: list[dict[str, Any]] = []


class PreopenDecisionSessionCta(BaseModel):
    state: PreopenDecisionSessionCtaState
    label: str
    run_uuid: UUID | None = None
    linked_session_uuid: UUID | None = None
    disabled_reason: str | None = None
    requires_confirmation: bool = True


class PreopenBriefingArtifact(BaseModel):
    artifact_type: Literal["preopen_briefing"] = "preopen_briefing"
    artifact_version: Literal["v1"] = "v1"
    status: PreopenArtifactStatus
    run_uuid: UUID | None = None
    market_scope: Literal["kr", "us", "crypto"] | None = None
    stage: Literal["preopen"] | None = None
    generated_at: datetime | None = None
    source_run_status: str | None = None
    readiness: list[PreopenArtifactReadinessItem] = []
    market_summary: str | None = None
    news_summary: str | None = None
    sections: list[PreopenArtifactSection] = []
    risk_notes: list[str] = []
    cta: PreopenDecisionSessionCta
    qa: dict[str, Any] = {}


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
    news_brief: KRPreopenNewsBrief | None = None
    market_news_briefing: PreopenMarketNewsBriefing | None = None
    briefing_artifact: PreopenBriefingArtifact | None = None
    qa_evaluator: PreopenQaEvaluatorSummary | None = None
