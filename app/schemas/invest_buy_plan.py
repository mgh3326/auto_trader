"""Read-only schemas for the /invest 매수 계획 (트리거 보드) — §144차.

Every number on this board is a **display approximation of the policy
formula**, not a session verdict and not an order. The response therefore
carries its own provenance: the policy ``version``/``content_hash`` it was
computed against, the calculation time, and an explicit per-field source list.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

BuyPlanMarket = Literal["kr", "us", "crypto"]
BuyPlanCurrency = Literal["KRW", "USD"]
ApprovalLane = Literal["auto_submit", "human_card"]
ApprovalLaneReason = Literal[
    "within_tier_auto_submit_notional",
    "above_tier_auto_submit_notional",
    "above_per_order_auto_approve_cap",
    "notional_unavailable",
]
PlacementForm = Literal["resting_order", "watch"]
GateConditionState = Literal["met", "not_met", "unavailable"]
# ``indeterminate`` is not a softer "closed": the policy says a missing
# threshold must not be inferred or counted as met, so an unreadable input
# leaves the gate un-passable but also un-proven.
GateState = Literal["open", "closed", "indeterminate"]
FundingVerdict = Literal["sufficient", "shortfall", "unknown"]


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class BuyPlanDecimalModel(BaseModel):
    """Base that serialises every Decimal field as an exact string."""

    model_config = ConfigDict(extra="forbid")


class PolicyStamp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    content_hash: str


class ValueSource(BaseModel):
    """Where one block of the board got its numbers from."""

    model_config = ConfigDict(extra="forbid")

    field: str
    source: str
    note: str | None = None


class AveragingSampleRow(BuyPlanDecimalModel):
    """One sampled depth below the A(k) turn point."""

    offset_from_turn_point_pct: Decimal
    price: Decimal
    additional_notional: Decimal
    target_average_price: Decimal
    approval_lane: ApprovalLane
    approval_lane_reason: ApprovalLaneReason

    @field_serializer(
        "offset_from_turn_point_pct",
        "price",
        "additional_notional",
        "target_average_price",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class AveragingTriggerRow(BuyPlanDecimalModel):
    """One underwater lot's 물타기 A(k) 전환점."""

    market: BuyPlanMarket
    symbol: str
    symbol_name: str | None = None
    currency: BuyPlanCurrency
    account_sources: list[str] = Field(default_factory=list)
    quantity: Decimal
    average_price: Decimal
    cost_basis: Decimal
    current_price: Decimal
    unrealized_pnl_pct: Decimal | None = None
    k: Decimal
    turn_point_price: Decimal
    distance_to_turn_point_pct: Decimal
    turn_point_reached: bool
    samples: list[AveragingSampleRow] = Field(default_factory=list)
    # The deeper sample — what to keep in reserve if this trigger fires.
    reserve_plan_notional: Decimal
    # Rank within its market by nearness to the turn point. The policy caps
    # A(k) adds at ``max_add_symbols_per_market``; rows beyond the cap are
    # shown but excluded from the funding total.
    market_rank: int
    within_policy_add_cap: bool
    notes: list[str] = Field(default_factory=list)

    @field_serializer(
        "quantity",
        "average_price",
        "cost_basis",
        "current_price",
        "unrealized_pnl_pct",
        "k",
        "turn_point_price",
        "distance_to_turn_point_pct",
        "reserve_plan_notional",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class SupportNetPlacement(BuyPlanDecimalModel):
    """One already-expressed rung of a support net — resting order or watch."""

    form: PlacementForm
    reference: str
    anchor_price: Decimal | None = None
    quantity: Decimal | None = None
    notional: Decimal | None = None
    distance_from_current_pct: Decimal | None = None
    valid_until: dt.datetime | None = None
    within_policy_distance_band: bool | None = None

    @field_serializer(
        "anchor_price", "quantity", "notional", "distance_from_current_pct"
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class SupportNetRow(BuyPlanDecimalModel):
    """One held coin's 지지 그물 티어 state."""

    market: BuyPlanMarket
    symbol: str
    symbol_name: str | None = None
    currency: BuyPlanCurrency
    quantity: Decimal
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    unrealized_pnl_pct: Decimal | None = None
    eligible: bool
    ineligible_reason: str | None = None
    placements: list[SupportNetPlacement] = Field(default_factory=list)
    placed_notional: Decimal
    per_symbol_cap_notional: Decimal
    remaining_headroom_notional: Decimal

    @field_serializer(
        "quantity",
        "average_price",
        "current_price",
        "unrealized_pnl_pct",
        "placed_notional",
        "per_symbol_cap_notional",
        "remaining_headroom_notional",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class SupportNetTier(BuyPlanDecimalModel):
    """Tier-level totals for ``buy.held_majors_support_net``."""

    policy_key: str
    enabled: bool
    currency: BuyPlanCurrency
    tier_cap_notional: Decimal | None = None
    per_symbol_cap_notional: Decimal | None = None
    placed_notional: Decimal
    remaining_notional: Decimal | None = None
    distance_band_pct: list[Decimal] = Field(default_factory=list)
    review_date: str | None = None
    rows: list[SupportNetRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_serializer(
        "tier_cap_notional",
        "per_symbol_cap_notional",
        "placed_notional",
        "remaining_notional",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)

    @field_serializer("distance_band_pct")
    def _ser_band(self, value: list[Decimal]) -> list[str]:
        return [format(v, "f") for v in value]


class ActiveBuyWatchRow(BuyPlanDecimalModel):
    """An active buy-side watch — cash that becomes needed when it fires."""

    market: BuyPlanMarket
    symbol: str
    symbol_name: str | None = None
    currency: BuyPlanCurrency
    alert_uuid: str
    metric: str
    operator: Literal["above", "below", "between"]
    threshold: Decimal
    threshold_high: Decimal | None = None
    current_price: Decimal | None = None
    distance_to_threshold_pct: Decimal | None = None
    valid_until: dt.datetime
    near_expiry: bool = False
    planned_notional: Decimal | None = None
    planned_notional_source: str | None = None
    approval_lane: ApprovalLane
    approval_lane_reason: ApprovalLaneReason

    @field_serializer(
        "threshold",
        "threshold_high",
        "current_price",
        "distance_to_threshold_pct",
        "planned_notional",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class DiscoveryGateCondition(BuyPlanDecimalModel):
    condition_id: str
    metric: str
    comparison: str | None = None
    threshold: Decimal | None = None
    unit: str | None = None
    current_value: Decimal | None = None
    state: GateConditionState
    source: str | None = None
    note: str | None = None

    @field_serializer("threshold", "current_value")
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class DiscoveryGateRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: BuyPlanMarket
    gate_key: str
    state: GateState
    min_conditions_met: int
    of: int
    met_count: int
    unavailable_count: int
    semantics: str | None = None
    conditions: list[DiscoveryGateCondition] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CashAccountRow(BuyPlanDecimalModel):
    account_id: str
    display_name: str
    source: str
    currency: BuyPlanCurrency
    available_cash: Decimal | None = None
    available_cash_source: str
    included_in_reserve: bool

    @field_serializer("available_cash")
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class CurrencyReconciliation(BuyPlanDecimalModel):
    currency: BuyPlanCurrency
    available_cash: Decimal | None = None
    required_averaging_adds: Decimal
    required_support_net: Decimal
    required_active_watches: Decimal
    required_total: Decimal
    verdict: FundingVerdict
    shortfall: Decimal | None = None
    notes: list[str] = Field(default_factory=list)

    @field_serializer(
        "available_cash",
        "required_averaging_adds",
        "required_support_net",
        "required_active_watches",
        "required_total",
        "shortfall",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class BuyPlanFunding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[CashAccountRow] = Field(default_factory=list)
    currencies: list[CurrencyReconciliation] = Field(default_factory=list)


class BuyPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: dt.datetime
    policy: PolicyStamp
    cache_ttl_seconds: int
    approximation_notice: str
    market: Literal["all", "kr", "us", "crypto"]
    averaging_triggers: list[AveragingTriggerRow] = Field(default_factory=list)
    support_net: SupportNetTier
    active_buy_watches: list[ActiveBuyWatchRow] = Field(default_factory=list)
    discovery_gates: list[DiscoveryGateRow] = Field(default_factory=list)
    funding: BuyPlanFunding
    value_sources: list[ValueSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
