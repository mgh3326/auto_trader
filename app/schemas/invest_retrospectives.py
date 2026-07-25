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
    # ROB-691 — trade-history filters (echoed so the client can confirm what
    # was actually applied).
    outcome_filter: str | None = None
    q: str | None = None
    kst_date_from: str | None = None
    kst_date_to: str | None = None
    count: int = Field(ge=0)
    total: int = Field(ge=0)
    items: list[RetrospectiveRow]
    as_of: datetime


# ROB-691 — judgment scoreboard (win-rate / realized-PnL / win-loss), mirroring
# build_retrospective_aggregate's group dict field-for-field (extra="ignore":
# the service returns a superset the aggregate already computes; we keep a
# stable subset here).
class ScoreboardGroupRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    group: str
    sample_size: int = Field(ge=0)
    wins: int = Field(ge=0)
    misses: int = Field(ge=0)
    win_rate_pct: float | None = None
    avg_pnl_pct: float | None = None
    realized_pnl_sum: dict[str, float] = Field(default_factory=dict)
    fx_pnl_krw_sum: float = 0.0
    total_pnl_krw_sum: float = 0.0
    by_outcome: dict[str, int] = Field(default_factory=dict)
    by_trigger_type: dict[str, int] = Field(default_factory=dict)
    by_root_cause_class: dict[str, int] = Field(default_factory=dict)


class ScoreboardTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_size: int = Field(ge=0)
    wins: int = Field(ge=0)
    misses: int = Field(ge=0)
    decided: int = Field(ge=0)
    win_rate_pct: float | None = None
    realized_pnl_sum: dict[str, float] = Field(default_factory=dict)
    fx_pnl_krw_sum: float = 0.0
    total_pnl_krw_sum: float = 0.0
    excluded_no_fill_evidence: int = Field(ge=0)


class ScoreboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_by: str
    market: Literal["all", "kr", "us", "crypto"]
    kst_date_from: str | None = None
    kst_date_to: str | None = None
    count: int = Field(ge=0)
    groups: list[ScoreboardGroupRow]
    totals: ScoreboardTotals
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
    action_id: str | None = None
    version: int | None = None
    overdue: bool = False
    terminal_status: str | None = None


class NextActionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["all", "kr", "us", "crypto"]
    symbol: str | None = None
    count: int = Field(ge=0)
    scan_limit: int = Field(ge=0)
    items: list[NextActionRow]


class CanonicalActionRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str
    version: int
    action: str
    owner: str | None = None
    issue_id: str | None = None
    status: str
    due_kst_date: str | None = None
    overdue: bool = False
    status_changed_at: str | None = None
    resolved_at: str | None = None
    status_actor: str | None = None
    status_source: str | None = None
    status_reason: str | None = None
    retrospective_id: int
    correlation_id: str | None = None
    symbol: str | None = None
    market: str | None = None
    trigger_type: str | None = None
    outcome: str | None = None
    realized_pnl: float | None = None
    created_at: str | None = None


class CanonicalActionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    count: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    as_of: datetime
    items: list[CanonicalActionRow]
