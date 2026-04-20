from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DecisionRunResponse(BaseModel):
    id: str
    generated_at: datetime
    mode: Literal["analysis_only", "dry_run", "live"] = "analysis_only"
    persisted: bool = False
    source: str = "portfolio_decision_service_v1"


class DecisionFiltersResponse(BaseModel):
    market: str = "ALL"
    account_keys: list[str] = Field(default_factory=list)
    q: str | None = None


class DecisionSummaryResponse(BaseModel):
    symbols: int = 0
    decision_items: int = 0
    actionable_items: int = 0
    manual_review_items: int = 0
    auto_candidate_items: int = 0
    missing_context_items: int = 0
    by_action: dict[str, int] = Field(default_factory=dict)
    by_market: dict[str, int] = Field(default_factory=dict)


class DecisionFacetsResponse(BaseModel):
    accounts: list[dict[str, Any]] = Field(default_factory=list)


class DecisionPositionContextResponse(BaseModel):
    quantity: float | None = None
    avg_price: float | None = None
    current_price: float | None = None
    evaluation: float | None = None
    evaluation_krw: float | None = None
    profit_loss: float | None = None
    profit_loss_krw: float | None = None
    profit_rate: float | None = None
    portfolio_weight_pct: float | None = None
    market_weight_pct: float | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)


class DecisionJournalContextResponse(BaseModel):
    status: str | None = None
    strategy: str | None = None
    target_price: float | None = None
    stop_loss: float | None = None
    target_distance_pct: float | None = None
    stop_distance_pct: float | None = None


class SupportResistanceLevelResponse(BaseModel):
    price: float
    distance_pct: float
    strength: str
    sources: list[str] = Field(default_factory=list)


class SupportResistanceContextResponse(BaseModel):
    status: Literal["available", "unavailable", "pending"]
    nearest_support: SupportResistanceLevelResponse | None = None
    nearest_resistance: SupportResistanceLevelResponse | None = None
    supports: list[SupportResistanceLevelResponse] = Field(default_factory=list)
    resistances: list[SupportResistanceLevelResponse] = Field(default_factory=list)


class ExecutionBoundaryResponse(BaseModel):
    mode: Literal["analysis_only", "manual_only", "dry_run_ready", "live_ready"]
    channel: str | None = None
    auto_executable: bool = False
    manual_only: bool = False
    reason: str | None = None
    future_capability: str | None = None


class DecisionAnchorResponse(BaseModel):
    type: str
    price: float | None = None
    distance_pct: float | None = None
    strength: str | None = None


class DecisionItemResponse(BaseModel):
    id: str
    action: Literal[
        "buy_candidate", "trim_candidate", "sell_watch", "hold", "manual_review"
    ]
    label: str
    priority: Literal["low", "medium", "high"] = "low"
    current_price: float | None = None
    action_price: float | None = None
    action_price_source: str | None = None
    delta_from_current_pct: float | None = None
    anchor: DecisionAnchorResponse | None = None
    rationale: list[str] = Field(default_factory=list)
    execution_boundary: ExecutionBoundaryResponse
    badges: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DecisionSymbolGroupResponse(BaseModel):
    market_type: str
    symbol: str
    name: str
    detail_url: str
    position: DecisionPositionContextResponse
    journal: DecisionJournalContextResponse | None = None
    support_resistance: SupportResistanceContextResponse
    items: list[DecisionItemResponse] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PortfolioDecisionSlateResponse(BaseModel):
    success: bool = True
    decision_run: DecisionRunResponse
    filters: DecisionFiltersResponse
    summary: DecisionSummaryResponse
    facets: DecisionFacetsResponse
    symbol_groups: list[DecisionSymbolGroupResponse]
    warnings: list[str] = Field(default_factory=list)
