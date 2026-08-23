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
# A read model that told us it was incomplete. Kept as a first-class value so
# a degraded upstream can never render as a confident zero (verify-r1 B3/B4).
SourceState = Literal["ok", "degraded", "unavailable"]
# Canonical broker identity for cash scoping (verify-r1 B1). Cash in one
# broker cannot fund an order placed at another, so every reconciliation is
# keyed by (broker, currency) and never by currency alone.
FundingBroker = Literal["kis", "upbit", "toss", "unattributed"]


def _decimal_str(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class BuyPlanDecimalModel(BaseModel):
    """Base that serialises every Decimal field as an exact string."""

    model_config = ConfigDict(extra="forbid")


class PolicyStamp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    content_hash: str


class ApprovalCondition(BaseModel):
    """One auto-approval gate this board does not evaluate.

    ``code`` is the literal ``reject(...)`` reason from
    ``order_proposals.auto_approve.evaluate_auto_approve_eligibility``; a
    contract test extracts those literals from the source and fails if this
    list stops covering them. A partial list is its own lie — it reads as
    "these are the only things we skipped".
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str


class ApprovalContext(BaseModel):
    """What the board can and cannot say about the auto-approval lane.

    ``approval_lane`` on a row is a **cap-only** classification: it compares a
    projected notional against the tier auto-submit ceiling and the per-order
    auto-approve cap. Real dispatch additionally requires the master gate below
    plus a set of conditions this read surface does not evaluate
    (``unevaluated_conditions``). Publishing that gap is the whole point of
    this object — "cap 이하 = 자동승인" is false (verify-r1 B6).
    """

    model_config = ConfigDict(extra="forbid")

    master_gate_enabled: bool | None
    master_gate_setting: str
    master_gate_source: str
    unevaluated_conditions: list[ApprovalCondition] = Field(default_factory=list)
    evaluated_conditions: list[ApprovalCondition] = Field(default_factory=list)
    notice: str


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
    funding_broker: FundingBroker
    funding_broker_reason: str | None = None
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
    # ``None`` when a placement source was degraded/unavailable: an unknown
    # amount is not zero, and a headroom computed from a partial order list
    # would overstate what is still free to spend (verify-r1 B4).
    placed_notional: Decimal | None = None
    per_symbol_cap_notional: Decimal
    remaining_headroom_notional: Decimal | None = None
    placements_state: SourceState = "ok"
    placements_incomplete_reasons: list[str] = Field(default_factory=list)

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
    placed_notional: Decimal | None = None
    remaining_notional: Decimal | None = None
    placements_state: SourceState = "ok"
    placements_incomplete_reasons: list[str] = Field(default_factory=list)
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
    # Which broker account would have to hold this cash. ``unattributed`` means
    # the watch's execution plan did not name one, so the amount cannot be
    # charged to any single account's reserve (verify-r1 B1).
    funding_broker: FundingBroker
    # Why attribution failed, verbatim. Collapsing every failure into "no
    # account_mode" hid genuinely wrong values like ``kiwoom_mock``
    # (verify-r2 SHOULD-3).
    funding_broker_reason: str | None = None
    account_mode: str | None = None
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
    # Canonical broker this account belongs to; cash is only ever compared
    # with requirements carrying the same broker (verify-r1 B1).
    broker: FundingBroker
    currency: BuyPlanCurrency
    available_cash: Decimal | None = None
    available_cash_source: str
    included_in_reserve: bool

    @field_serializer("available_cash")
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class ScopeReconciliation(BuyPlanDecimalModel):
    """Cash vs requirements for ONE (broker, currency) pair.

    Scoping by broker is not cosmetic: KIS KRW cannot fill an Upbit order, so
    a currency-only total can read ``sufficient`` while the account that must
    place the order holds nothing (verify-r1 B1).

    ``broker="unattributed"`` is a real destination row carrying the
    requirements whose owning account could not be resolved. It always exists
    when such a requirement exists, including for a currency or broker that
    has no cash account at all — otherwise an unresolved need could reach no
    verdict anywhere on the board (verify-r2 B1 (b)/(d)).

    A scope with same-currency unattributed money can never read
    ``sufficient``: that money may land at a *different* broker, so this
    account covering it proves nothing about the account that will actually
    place the order (verify-r2 B1 (a)).
    """

    scope_key: str
    broker: FundingBroker
    currency: BuyPlanCurrency
    account_ids: list[str] = Field(default_factory=list)
    available_cash: Decimal | None = None
    required_averaging_adds: Decimal
    required_support_net: Decimal
    required_active_watches: Decimal
    required_total: Decimal
    unattributed_same_currency: Decimal
    # Deliberately NOT called a worst case. The true worst case is that the
    # unattributed money lands on an account with no cash, which is a
    # different scope entirely — this number only answers the conditional
    # "if all of it landed here" (verify-r2 3-1(e)).
    upper_bound_if_all_unattributed_lands_here: Decimal
    requirements_complete: bool = True
    incomplete_reasons: list[str] = Field(default_factory=list)
    verdict: FundingVerdict
    shortfall: Decimal | None = None
    notes: list[str] = Field(default_factory=list)

    @field_serializer(
        "available_cash",
        "required_averaging_adds",
        "required_support_net",
        "required_active_watches",
        "required_total",
        "unattributed_same_currency",
        "upper_bound_if_all_unattributed_lands_here",
        "shortfall",
    )
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class UnattributedRequirement(BuyPlanDecimalModel):
    """One requirement the board could not pin to a broker account."""

    kind: Literal["averaging_add", "support_net", "active_watch"]
    label: str
    currency: BuyPlanCurrency
    amount: Decimal | None = None
    reason: str

    @field_serializer("amount")
    def _ser(self, value: Decimal | None) -> str | None:
        return _decimal_str(value)


class BuyPlanFunding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[CashAccountRow] = Field(default_factory=list)
    scopes: list[ScopeReconciliation] = Field(default_factory=list)
    unattributed: list[UnattributedRequirement] = Field(default_factory=list)
    source_warnings: list[str] = Field(default_factory=list)


class BuyPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: dt.datetime
    policy: PolicyStamp
    cache_ttl_seconds: int
    approximation_notice: str
    approval_context: ApprovalContext
    market: Literal["all", "kr", "us", "crypto"]
    averaging_triggers: list[AveragingTriggerRow] = Field(default_factory=list)
    support_net: SupportNetTier
    active_buy_watches: list[ActiveBuyWatchRow] = Field(default_factory=list)
    discovery_gates: list[DiscoveryGateRow] = Field(default_factory=list)
    funding: BuyPlanFunding
    value_sources: list[ValueSource] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
