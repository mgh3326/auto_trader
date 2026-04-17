"""Schemas for TC/CIO board brief follow-up rendering."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FundingIntent = Literal["runway_recovery", "new_buy", "partial", "other"]
BoardBriefPhase = Literal["tc_preliminary", "cio_pending"]
GateStatus = Literal["pass", "fail", "pending", "tbd"]


class GateResult(BaseModel):
    """Generic gate result row for board-facing brief output."""

    status: GateStatus = Field(..., description="Gate status label")
    detail: str = Field("", description="Human-readable gate detail")


class N8nG2GatePayload(BaseModel):
    """G2 funding-intent gate output consumed by CIO pending brief rendering."""

    passed: bool = Field(..., description="Whether G2 allows the requested intent")
    status: GateStatus = Field("pass", description="Normalized status label")
    blocking_reason: str | None = Field(None, description="Reason when G2 fails")
    detail: str | None = Field(None, description="Optional human-readable detail")


class WeightItem(BaseModel):
    """Portfolio concentration item."""

    symbol: str = Field(..., min_length=1)
    weight_pct: float = Field(..., ge=0)


class HoldingSnapshot(BaseModel):
    """Board brief holding snapshot."""

    symbol: str = Field(..., min_length=1)
    current_krw_value: float = Field(0, ge=0)
    pnl_pct: float | None = None
    dust: bool = False


class DustItem(BaseModel):
    """Dust asset line item."""

    symbol: str = Field(..., min_length=1)
    current_krw_value: float = Field(0, ge=0)
    dust: bool = True


class BoardFundingResponse(BaseModel):
    """Board funding response captured between TC preliminary and CIO pending."""

    amount: float = Field(..., gt=0, le=100_000_000_000)
    target: str | None = Field(None, max_length=80)
    funding_intent: FundingIntent
    manual_cash_verified: bool = False


class BoardBriefContext(BaseModel):
    """Internal render context shared by TC preliminary and CIO pending builders."""

    manual_cash_krw: float = Field(0, ge=0)
    daily_burn_krw: float = Field(0, ge=0)
    manual_cash_runway_days: float | None = Field(None, ge=0)
    funding_intent: FundingIntent | None = None
    board_response: BoardFundingResponse | None = None
    g1_gate: dict[str, Any] | None = None
    gate_results: dict[str, GateResult | N8nG2GatePayload] = Field(default_factory=dict)
    weights_top_n: list[WeightItem] = Field(default_factory=list)
    holdings: list[HoldingSnapshot] = Field(default_factory=list)
    dust_items: list[DustItem] = Field(default_factory=list)
    generated_at: datetime | None = None


class BoardBriefRender(BaseModel):
    """Endpoint response schema for board brief follow-up renders."""

    phase: BoardBriefPhase
    embed: dict[str, Any]
    text: str
    gate_results: dict[str, GateResult | N8nG2GatePayload] | None = None
    generated_at: datetime


class TCFollowupRequest(BaseModel):
    """Request payload for /api/n8n/tc-followup."""

    manual_cash_krw: float = Field(0, ge=0)
    daily_burn_krw: float = Field(0, ge=0)
    manual_cash_runway_days: float | None = Field(None, ge=0)
    weights_top_n: list[WeightItem] = Field(default_factory=list)
    holdings: list[HoldingSnapshot] = Field(default_factory=list)
    dust_items: list[DustItem] = Field(default_factory=list)
    generated_at: datetime | None = None


class CIOFollowupRequest(TCFollowupRequest):
    """Request payload for /api/n8n/cio-followup."""

    funding_intent: FundingIntent | None = None
    board_response: BoardFundingResponse | None = None
    g1_gate: dict[str, Any] | None = None


__all__ = [
    "BoardBriefContext",
    "BoardBriefPhase",
    "BoardBriefRender",
    "BoardFundingResponse",
    "CIOFollowupRequest",
    "DustItem",
    "FundingIntent",
    "GateResult",
    "N8nG2GatePayload",
    "TCFollowupRequest",
    "WeightItem",
]
