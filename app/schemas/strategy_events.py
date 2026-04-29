"""ROB-41 strategy event request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

StrategyEventSourceLiteral = Literal[
    "user", "hermes", "tradingagents", "news", "market_data", "scheduler"
]
StrategyEventTypeLiteral = Literal[
    "operator_market_event",
    "earnings_event",
    "macro_event",
    "sector_rotation",
    "technical_break",
    "risk_veto",
    "cash_budget_change",
    "position_change",
]


def _strip_short(items: list[str], *, max_len: int) -> list[str]:
    cleaned: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("list entries must be strings")
        v = raw.strip()
        if not v:
            continue
        if len(v) > max_len:
            raise ValueError(f"entry exceeds {max_len} chars")
        cleaned.append(v)
    return cleaned


class StrategyEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: StrategyEventSourceLiteral = "user"
    event_type: StrategyEventTypeLiteral
    source_text: str = Field(min_length=1, max_length=8000)
    normalized_summary: str | None = Field(default=None, max_length=2000)
    session_uuid: UUID | None = None
    affected_markets: list[str] = Field(default_factory=list, max_length=32)
    affected_sectors: list[str] = Field(default_factory=list, max_length=32)
    affected_themes: list[str] = Field(default_factory=list, max_length=32)
    affected_symbols: list[str] = Field(default_factory=list, max_length=64)
    severity: int = Field(default=2, ge=1, le=5)
    confidence: int = Field(default=50, ge=0, le=100)
    metadata: dict | None = None  # stored in DB column `event_metadata`

    @field_validator("affected_markets", "affected_sectors", "affected_themes")
    @classmethod
    def _short_list(cls, v: list[str]) -> list[str]:
        return _strip_short(v, max_len=64)

    @field_validator("affected_symbols")
    @classmethod
    def _symbol_list(cls, v: list[str]) -> list[str]:
        return _strip_short(v, max_len=32)


class StrategyEventDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    event_uuid: UUID
    session_uuid: UUID | None
    source: StrategyEventSourceLiteral
    event_type: StrategyEventTypeLiteral
    source_text: str
    normalized_summary: str | None
    affected_markets: list[str]
    affected_sectors: list[str]
    affected_themes: list[str]
    affected_symbols: list[str]
    severity: int
    confidence: int
    created_by_user_id: int | None
    metadata: dict | None
    created_at: datetime


class StrategyEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[StrategyEventDetail]
    total: int
    limit: int
    offset: int
