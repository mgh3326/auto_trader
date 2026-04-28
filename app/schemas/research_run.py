"""Pydantic schemas for Research Run snapshot persistence."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trading_decisions import InstrumentTypeLiteral, SideLiteral

MarketScopeLiteral = Literal["kr", "us", "crypto"]
StageLiteral = Literal["preopen", "intraday", "nxt_aftermarket", "us_open"]
RunStatusLiteral = Literal["open", "closed", "archived"]
CandidateKindLiteral = Literal[
    "pending_order", "holding", "screener_hit", "proposed", "other"
]
ReconClassificationLiteral = Literal[
    "maintain",
    "near_fill",
    "too_far",
    "chasing_risk",
    "data_mismatch",
    "kr_pending_non_nxt",
    "unknown_venue",
    "unknown",
]
NxtClassificationLiteral = Literal[
    "buy_pending_at_support",
    "buy_pending_too_far",
    "buy_pending_actionable",
    "sell_pending_near_resistance",
    "sell_pending_too_optimistic",
    "sell_pending_actionable",
    "non_nxt_pending_ignore_for_nxt",
    "holding_watch_only",
    "data_mismatch_requires_review",
    "unknown",
]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_WARNING_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _AdvisoryLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    session_uuid: UUID | None = None
    note: str | None = Field(default=None, max_length=512)


class ResearchRunCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    candidate_kind: CandidateKindLiteral
    proposed_price: Decimal | None = Field(default=None, ge=0)
    proposed_qty: Decimal | None = Field(default=None, ge=0)
    confidence: int | None = Field(default=None, ge=0, le=100)
    rationale: str | None = Field(default=None, max_length=4000)
    currency: str | None = Field(default=None, max_length=8)
    source_freshness: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, v: str) -> str:
        if not _SYMBOL_RE.fullmatch(v):
            raise ValueError("symbol contains unsupported characters")
        return v

    @field_validator("warnings")
    @classmethod
    def _warning_charset(cls, v: list[str]) -> list[str]:
        for token in v:
            if not _WARNING_RE.fullmatch(token):
                raise ValueError(f"warning token not allowed: {token}")
        return v


class ResearchRunPendingReconciliationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: int | None = None
    order_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    market: MarketScopeLiteral
    side: Literal["buy", "sell"]
    classification: ReconClassificationLiteral
    nxt_classification: NxtClassificationLiteral | None = None
    nxt_actionable: bool | None = None
    gap_pct: Decimal | None = None
    reasons: list[str] = Field(default_factory=list, max_length=64)
    warnings: list[str] = Field(default_factory=list, max_length=64)
    decision_support: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=512)


class ResearchRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_scope: MarketScopeLiteral
    stage: StageLiteral
    source_profile: str = Field(min_length=1, max_length=64)
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    market_brief: dict[str, Any] | None = None
    source_freshness: dict[str, Any] | None = None
    source_warnings: list[str] = Field(default_factory=list, max_length=64)
    advisory_links: list[_AdvisoryLink] = Field(default_factory=list, max_length=20)
    generated_at: datetime
    candidates: list[ResearchRunCandidateCreate] = Field(
        default_factory=list, max_length=200
    )


class ResearchRunSummary(BaseModel):
    run_uuid: UUID
    market_scope: MarketScopeLiteral
    stage: StageLiteral
    status: RunStatusLiteral
    source_profile: str
    strategy_name: str | None
    generated_at: datetime
    candidate_count: int
    reconciliation_count: int
    source_warnings: list[str]


class ResearchRunDetail(ResearchRunSummary):
    notes: str | None
    market_brief: dict[str, Any] | None
    source_freshness: dict[str, Any] | None
    advisory_links: list[dict[str, Any]]
    candidates: list[ResearchRunCandidateCreate]
    reconciliations: list[ResearchRunPendingReconciliationCreate]
