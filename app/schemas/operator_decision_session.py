"""Operator-facing Trading Decision Session request/response schemas."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.trading_decisions import (
    InstrumentTypeLiteral,
    ProposalKindLiteral,
    SessionStatusLiteral,
    SideLiteral,
)

OperatorMarketScopeLiteral = Literal["kr", "us", "crypto"]

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._/-]{1,32}$")
_ANALYST_RE = re.compile(r"^[a-z_]{1,32}$")


class OperatorCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: InstrumentTypeLiteral
    side: SideLiteral = "none"
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindLiteral = "other"
    rationale: str = Field(default="", max_length=4000)
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    amount: Decimal | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    trigger_price: Decimal | None = Field(default=None, ge=0)
    threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    currency: str | None = Field(default=None, max_length=8)

    @field_validator("symbol")
    @classmethod
    def _symbol_charset(cls, value: str) -> str:
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol contains unsupported characters")
        return value


class OperatorDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_scope: OperatorMarketScopeLiteral
    candidates: list[OperatorCandidate] = Field(min_length=1, max_length=20)
    include_tradingagents: bool = False
    analysts: list[str] | None = None
    strategy_name: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    source_profile: str = Field(
        default="operator_request",
        min_length=1,
        max_length=64,
    )
    generated_at: datetime | None = None

    @field_validator("analysts")
    @classmethod
    def _analyst_charset(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for token in value:
            if not _ANALYST_RE.fullmatch(token):
                raise ValueError("analyst token contains unsupported characters")
        return value


class OperatorDecisionResponse(BaseModel):
    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None = None
