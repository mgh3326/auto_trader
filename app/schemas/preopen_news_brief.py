"""Pydantic schemas for the KR preopen Hermes news brief (ROB-62).

Advisory-only surface: no execution fields, no order fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RiskFlag(BaseModel):
    code: Literal[
        "news_stale",
        "news_unavailable",
        "ingestion_partial",
        "low_evidence",
        "tradingagents_unavailable",
    ]
    severity: Literal["info", "warn", "block_advisory_only"]
    message: str


class NewsRefRef(BaseModel):
    article_id: int
    title: str | None = None
    feed_source: str | None = None


class SectorImpactFlag(BaseModel):
    sector: str
    direction: Literal["positive", "negative", "mixed", "unclear"]
    confidence: int  # 0-100, capped by readiness
    sources: list[NewsRefRef] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)  # max 3


class CandidateImpactFlag(BaseModel):
    symbol: str  # KR symbol (DB '.' format)
    name: str
    direction: Literal["positive", "negative", "mixed", "unclear"]
    confidence: int  # 0-100
    sector: str | None = None
    reasons: list[str] = Field(default_factory=list)  # max 3
    research_run_candidate_id: int | None = None


class BriefConfidence(BaseModel):
    overall: int  # 0-100
    cap_reason: Literal[
        "news_stale", "news_unavailable", "no_tradingagents_evidence", "ok"
    ]


class KRPreopenNewsBrief(BaseModel):
    generated_at: datetime
    news_readiness: Literal["ok", "stale", "degraded", "unavailable"]
    news_max_age_minutes: int | None = None
    confidence: BriefConfidence
    sector_flags: list[SectorImpactFlag] = Field(default_factory=list)  # max ~5
    candidate_flags: list[CandidateImpactFlag] = Field(
        default_factory=list
    )  # max ~10, advisory-only
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    research_run_id: int | None = None
    advisory_only: Literal[True] = True
