"""Schemas for Research Run → Trading Decision Session integration."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.research_run import (
    CandidateKindLiteral,
    MarketScopeLiteral,
    NxtClassificationLiteral,
    ReconClassificationLiteral,
    RunStatusLiteral,
    StageLiteral,
)
from app.schemas.trading_decisions import SessionStatusLiteral


class ResearchRunSelector(BaseModel):
    """Selector for resolving a ResearchRun - either by UUID or by criteria."""

    model_config = ConfigDict(extra="forbid")

    run_uuid: UUID | None = None
    market_scope: MarketScopeLiteral | None = None
    stage: StageLiteral | None = None
    strategy_name: str | None = Field(default=None, max_length=128)
    status: RunStatusLiteral | None = "open"

    @model_validator(mode="after")
    def _xor(self) -> Self:
        """Ensure exactly one of run_uuid OR (market_scope + stage) is provided."""
        has_uuid = self.run_uuid is not None
        has_criteria = self.market_scope is not None and self.stage is not None
        if has_uuid and has_criteria:
            raise ValueError(
                "Provide either run_uuid OR (market_scope + stage), not both"
            )
        if not has_uuid and not has_criteria:
            raise ValueError("Provide either run_uuid OR (market_scope + stage)")
        return self


class ResearchRunDecisionSessionRequest(BaseModel):
    """Request to create a TradingDecisionSession from a ResearchRun."""

    model_config = ConfigDict(extra="forbid")

    selector: ResearchRunSelector
    include_tradingagents: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    generated_at: datetime | None = None  # default = now(UTC) at service layer


class LiveRefreshQuote(BaseModel):
    """Live quote data for a symbol."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal
    as_of: datetime


class OrderbookLevel(BaseModel):
    """Single level in an orderbook."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal
    quantity: Decimal


class OrderbookSnapshot(BaseModel):
    """Orderbook snapshot for a symbol."""

    model_config = ConfigDict(extra="forbid")

    best_bid: OrderbookLevel | None = None
    best_ask: OrderbookLevel | None = None
    total_bid_qty: Decimal | None = None
    total_ask_qty: Decimal | None = None


class SupportResistanceLevel(BaseModel):
    """Support or resistance level with distance."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal
    distance_pct: Decimal


class SupportResistanceSnapshot(BaseModel):
    """Support/resistance context for a symbol."""

    model_config = ConfigDict(extra="forbid")

    nearest_support: SupportResistanceLevel | None = None
    nearest_resistance: SupportResistanceLevel | None = None


class KrUniverseSnapshot(BaseModel):
    """KR symbol universe data for NXT eligibility."""

    model_config = ConfigDict(extra="forbid")

    nxt_eligible: bool
    name: str | None = None
    exchange: str | None = None


class VenueEligibility(BaseModel):
    """Venue eligibility for a symbol."""

    model_config = ConfigDict(extra="forbid")

    nxt: bool | None = None
    regular: bool | None = None


class PendingOrderSnapshot(BaseModel):
    """Live pending order data."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    ordered_price: Decimal
    ordered_qty: Decimal
    remaining_qty: Decimal
    currency: str | None = None
    ordered_at: datetime | None = None


class LiveRefreshSnapshot(BaseModel):
    """Complete live market snapshot for decision session creation."""

    model_config = ConfigDict(extra="forbid")

    refreshed_at: datetime
    quote_by_symbol: dict[str, LiveRefreshQuote] = Field(default_factory=dict)
    orderbook_by_symbol: dict[str, OrderbookSnapshot] = Field(default_factory=dict)
    support_resistance_by_symbol: dict[str, SupportResistanceSnapshot] = Field(
        default_factory=dict
    )
    kr_universe_by_symbol: dict[str, KrUniverseSnapshot] = Field(default_factory=dict)
    cash_balances: dict[str, Decimal] = Field(default_factory=dict)
    holdings_by_symbol: dict[str, Decimal] = Field(default_factory=dict)
    pending_orders: list[PendingOrderSnapshot] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ProposalPayload(BaseModel):
    """Payload structure for proposals created from research runs."""

    model_config = ConfigDict(extra="forbid")

    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    research_run_id: UUID
    research_run_candidate_id: int
    refreshed_at: datetime
    reconciliation_status: ReconClassificationLiteral | None = None
    reconciliation_summary: str | None = None
    nxt_classification: NxtClassificationLiteral | None = None
    nxt_summary: str | None = None
    nxt_eligible: bool | None = None
    venue_eligibility: dict[str, bool | None] = Field(default_factory=dict)
    live_quote: LiveRefreshQuote | None = None
    pending_order_id: str | None = None
    decision_support: dict[str, Any] = Field(default_factory=dict)
    source_freshness: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    candidate_kind: CandidateKindLiteral


class ResearchRunDecisionSessionResponse(BaseModel):
    """Response after creating a TradingDecisionSession from a ResearchRun."""

    model_config = ConfigDict(extra="forbid")

    session_uuid: UUID
    session_url: str
    status: SessionStatusLiteral
    research_run_uuid: UUID
    refreshed_at: datetime
    proposal_count: int
    reconciliation_count: int
    advisory_used: bool = False
    advisory_skipped_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
