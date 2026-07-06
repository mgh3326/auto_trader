"""ROB-516 session context DTOs for MCP and service boundaries."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.investment_reports import AccountScopeLiteral, MarketLiteral

SessionContextEntryTypeLiteral = Literal[
    "plan",
    "decision",
    "deferred",
    "rejected_candidate",
    "constraint",
    "open_question",
    "next_action",
    "handoff_note",
]
SessionContextCreatedByLiteral = Literal["claude", "operator", "system", "codex"]


class SessionContextRefs(BaseModel):
    report_uuid: UUID | None = None
    item_uuid: UUID | None = None
    alert_uuid: UUID | None = None
    order_id: str | None = None
    journal_id: int | None = None
    symbols: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("symbols", mode="before")
    @classmethod
    def _clean_symbols(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("symbols must be a list")
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        return cleaned


class SessionContextAppendEntry(BaseModel):
    kst_date: date | None = None
    market: MarketLiteral
    account_scope: AccountScopeLiteral | None = None
    entry_type: SessionContextEntryTypeLiteral
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    refs: SessionContextRefs = Field(default_factory=SessionContextRefs)
    created_by: SessionContextCreatedByLiteral = "claude"
    session_label: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("title", "body", "session_label", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped
        return value


class SessionContextRecentRequest(BaseModel):
    market: MarketLiteral | None = None
    account_scope: AccountScopeLiteral | None = None
    kst_date_from: date | None = None
    entry_type: SessionContextEntryTypeLiteral | None = None
    limit: int = Field(default=20, ge=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("limit", mode="before")
    @classmethod
    def _clamp_limit(cls, value: object) -> int:
        limit = 20 if value is None else int(value)
        return max(1, min(limit, 100))


class SessionContextResponse(BaseModel):
    entry_uuid: UUID
    kst_date: date
    market: MarketLiteral
    account_scope: AccountScopeLiteral | None
    entry_type: SessionContextEntryTypeLiteral
    title: str
    body: str
    refs: SessionContextRefs
    created_by: SessionContextCreatedByLiteral
    session_label: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionContextAppendResponse(BaseModel):
    success: Literal[True] = True
    count: int
    entries: list[SessionContextResponse]


class SessionContextRecentResponse(BaseModel):
    success: Literal[True] = True
    count: int
    filters: SessionContextRecentRequest
    entries: list[SessionContextResponse]
