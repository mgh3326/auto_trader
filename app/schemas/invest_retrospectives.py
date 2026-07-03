"""Read-only schemas for /invest retrospectives (ROB-662)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RetrospectiveRow(BaseModel):
    # extra=ignore: fed the full serialize_retrospective(...) dict; we keep a subset.
    model_config = ConfigDict(extra="ignore")

    id: int
    correlation_id: str | None = None
    symbol: str
    market: str | None = None
    instrument_type: str | None = None
    side: str | None = None
    trigger_type: str | None = None
    root_cause_class: str | None = None
    outcome: str | None = None
    realized_pnl: float | None = None
    realized_pnl_currency: str | None = None
    pnl_pct: float | None = None
    result_summary: str | None = None
    lesson: str | None = None
    next_strategy: str | None = None
    intended_vs_happened: dict[str, Any] | None = None
    next_actions: list[dict[str, Any]] | None = None
    guardrail_fired: bool | None = None
    policy_version: str | None = None
    created_at: str | None = None


class RetrospectivesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["all", "kr", "us", "crypto"]
    trigger_type: str | None = None
    root_cause_class: str | None = None
    symbol: str | None = None
    count: int = Field(ge=0)
    total: int = Field(ge=0)
    items: list[RetrospectiveRow]
    as_of: datetime


class NextActionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str
    owner: str | None = None
    issue_id: str | None = None
    status: str | None = None
    due_kst_date: str | None = None
    symbol: str
    market: str | None = None
    retro_id: int
    correlation_id: str | None = None
    trigger_type: str | None = None
    realized_pnl: float | None = None
    created_at: str | None = None


class NextActionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["all", "kr", "us", "crypto"]
    symbol: str | None = None
    count: int = Field(ge=0)
    scan_limit: int = Field(ge=0)
    items: list[NextActionRow]
