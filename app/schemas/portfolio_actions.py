"""ROB-116 — Portfolio action board DTOs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CandidateAction = Literal["sell", "trim", "hold", "add", "watch"]
SummaryDecision = Literal["buy", "hold", "sell"]
MarketVerdict = Literal["bull", "bear", "neutral", "unavailable"]
JournalStatus = Literal["present", "missing", "stale"]


class PortfolioActionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    market: Literal["KR", "US", "CRYPTO"]
    instrument_type: str | None = None

    position_weight_pct: float | None = None
    profit_rate: float | None = None
    quantity: float | None = None
    sellable_quantity: float | None = None
    staked_quantity: float | None = None

    latest_research_session_id: int | None = None
    summary_decision: SummaryDecision | None = None
    summary_confidence: int | None = Field(default=None, ge=0, le=100)
    market_verdict: MarketVerdict | None = None

    nearest_support_pct: float | None = None
    nearest_resistance_pct: float | None = None
    journal_status: JournalStatus = "missing"

    candidate_action: CandidateAction
    suggested_trim_pct: int | None = None
    reason_codes: list[str]
    missing_context_codes: list[str]


class PortfolioActionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    total: int
    candidates: list[PortfolioActionCandidate]
    warnings: list[str] = Field(default_factory=list)
