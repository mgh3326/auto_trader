"""Schemas for TC/CIO board brief follow-up rendering."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FundingIntent = Literal["runway_recovery", "new_buy", "partial", "other"]
BoardBriefPhase = Literal["tc_preliminary", "cio_pending"]
GateStatus = Literal["pass", "fail", "pending", "tbd"]
BtcCloseVs20dMa = Literal["above", "below"]
BtcMa20Slope = Literal["up", "flat", "down"]
TierLabel = Literal["T1", "T2", "T3"]


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


class UnverifiedCapPayload(BaseModel):
    """Manual funding cap that is not counted as verified exchange cash."""

    amount: float = Field(0, ge=0)
    confirmed_at: datetime | None = None
    verified_by_boss_today: bool = False
    stale_warning: bool = False


class NextObligationPayload(BaseModel):
    """Next cash obligation used for runway and funding-path decisions."""

    date: date
    days_remaining: int = Field(..., ge=0)
    cash_needed_until: float = Field(..., ge=0)


class TierScenario(BaseModel):
    """Funding tier scenario for board-facing path A comparison."""

    label: TierLabel
    target_exchange_krw: float = Field(..., ge=0)
    deposit_amount: float = Field(..., ge=0)
    buffer_days: int = Field(..., ge=0)
    cushion_after_obligation: float


class HardGateCandidate(BaseModel):
    """Candidate action that still requires a separate hard-gate critique."""

    symbol: str = Field(..., min_length=1)
    proposal: str = Field(..., min_length=1)
    amount_range: str = Field(..., min_length=1)


class BtcRegimePayload(BaseModel):
    """BTC regime metrics used by G4."""

    close_vs_20d_ma: BtcCloseVs20dMa
    ma20_slope: BtcMa20Slope
    drawdown_14d_pct: float


class BoardBriefV2Fields(BaseModel):
    """Prompt v2 fields shared by context and n8n follow-up request bodies."""

    exchange_krw: float = Field(0, ge=0)
    unverified_cap: UnverifiedCapPayload | None = None
    next_obligation: NextObligationPayload | None = None
    tier_scenarios: list[TierScenario] = Field(default_factory=list)
    hard_gate_candidates: list[HardGateCandidate] = Field(default_factory=list)
    data_sufficient_by_symbol: dict[str, bool] = Field(default_factory=dict)
    btc_regime: BtcRegimePayload | None = None


class BoardBriefContext(BoardBriefV2Fields):
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


class TCFollowupRequest(BoardBriefV2Fields):
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
    "BtcRegimePayload",
    "CIOFollowupRequest",
    "DustItem",
    "FundingIntent",
    "GateResult",
    "HardGateCandidate",
    "N8nG2GatePayload",
    "NextObligationPayload",
    "TCFollowupRequest",
    "TierScenario",
    "UnverifiedCapPayload",
    "WeightItem",
]
