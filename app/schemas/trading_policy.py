"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Lane = Literal["buy", "sell", "discovery"]
Market = Literal["kr", "us", "crypto"]
PostureStateName = Literal[
    "RESTING",
    "CONDITIONAL",
    "ARMED_DEFERRED",
    "DISARMED",
    "EXPIRED_REARMABLE",
]

ThresholdValue = int | float | str | list[int | float]
RuleConditionValue = int | float | str | bool | list[int | float | str | bool]
PolicyComparison = Literal["gt", "gte", "lt", "lte", "eq"]
KrBroker = Literal["kis", "toss"]


class OneShareExceptionPolicy(BaseModel):
    """ROB-956 — US shares can't be bought fractionally; if a single share's
    price exceeds a USD notional band's ceiling, allow exactly one share
    instead of blocking the entry outright. absolute_ceiling_usd still hard-
    blocks ultra-high-priced symbols (BRK.A/NVR-class); max_deep_rungs caps
    additional averaging-down exposure on exception entries."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    absolute_ceiling_usd: float
    max_deep_rungs: int


class PolicyThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    value: ThresholdValue
    unit: str
    semantics: str
    of: int | None = None
    one_share_exception: OneShareExceptionPolicy | None = None


class PolicyDecisionRuleTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    conditions: dict[str, RuleConditionValue]
    action: str
    sizing: str


class PolicyDecisionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    semantics: str
    tiers: list[PolicyDecisionRuleTier]
    tie_breaks: dict[str, str] = Field(default_factory=dict)
    exclusions: list[str] = Field(default_factory=list)


class SingleShareExitScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[Literal["kr"]]
    brokers: list[KrBroker]
    required_broker_inventory: list[KrBroker]
    order_routable_required: Literal[True]

    @model_validator(mode="after")
    def validate_kis_toss_scope(self) -> SingleShareExitScope:
        required = {"kis", "toss"}
        if set(self.brokers) != required or len(self.brokers) != len(required):
            raise ValueError("brokers must contain exactly kis and toss")
        if set(self.required_broker_inventory) != required or len(
            self.required_broker_inventory
        ) != len(required):
            raise ValueError(
                "required_broker_inventory must contain exactly kis and toss"
            )
        return self


class SingleShareResistanceSourceFamilies(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_profile_exact: list[str]
    fibonacci_prefixes: list[str]
    bollinger_prefixes: list[str]


class SingleShareExitConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol_routable_sellable_quantity_eq: Literal[1]
    profit_pct_min: float = Field(ge=0)
    resistance_reference_required: Literal[True]
    resistance_strength_min: Literal["strong"]
    resistance_distance_pct_min_exclusive: float = Field(ge=0, le=100)
    resistance_distance_pct_max: float = Field(ge=0, le=100)
    resistance_source_family_min: int = Field(ge=2)
    resistance_source_families: SingleShareResistanceSourceFamilies
    quote_max_age_seconds: int = Field(gt=0)
    resistance_max_age_seconds: int = Field(gt=0)
    holdings_max_age_seconds: int = Field(gt=0)
    open_orders_max_age_seconds: int = Field(gt=0)
    open_actions_max_age_seconds: int = Field(gt=0)
    captured_at_max_age_seconds: int = Field(gt=0)
    snapshot_max_skew_seconds: int = Field(gt=0)
    required_completed_bar_market: Literal["XKRX"]
    min_sell_price_multiple_policy_key: Literal["sell.loss_guard_min_multiple"]
    same_symbol_open_orders_max: Literal[0]
    unresolved_open_actions_max: Literal[0]
    loss_state_uses_existing_path: Literal["loss_cut_only"]

    @model_validator(mode="after")
    def validate_resistance_band(self) -> SingleShareExitConditions:
        if (
            self.resistance_distance_pct_max
            <= self.resistance_distance_pct_min_exclusive
        ):
            raise ValueError("resistance distance max must exceed exclusive min")
        return self


class SingleShareExitProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["full_exit_at_far_resistance"]
    sizing: Literal["full_account_lot_exit"]
    approval: Literal["telegram_manual"]
    auto_approve: Literal[False]
    execution: Literal["proposal_only"]


class SingleShareExitDecisionRule(BaseModel):
    """Deprecated general fallback retained as a KR shadow/replay policy.

    This rule intentionally has a distinct shape from the tiered trim rule:
    ``sell.trim_preplace`` now includes one-share positions for a full-exit
    advisory review, while this legacy path can only classify the narrower KR
    far-resistance shadow cohort while ``proposal_enabled`` is false. Its
    candidate metadata is manual-approval-only for a separately authorized
    future activation; this schema never enables an order.
    """

    model_config = ConfigDict(extra="forbid")

    lanes: list[Literal["sell"]]
    semantics: str
    activation_state: Literal["shadow"]
    proposal_enabled: Literal[False]
    scope: SingleShareExitScope
    conditions: SingleShareExitConditions
    proposal: SingleShareExitProposal
    threshold_status: Literal["provisional"]
    operator_approval_required: Literal[True]
    recalibration_note: str


class PolicyRecoveryCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    metric: str
    sources: list[str]
    operator: PolicyComparison | None
    threshold: int | float | None
    unit: str
    semantics: str


class PolicyRecoveryGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    min_conditions_met: int
    of: int
    missing_or_null_threshold: str
    conditions: list[PolicyRecoveryCondition]
    advisory_context: list[PolicyRecoveryCondition] = Field(default_factory=list)


class PolicySupportResistanceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    selection_rule: str
    source_priority: list[str]
    confluence_examples: list[list[str]]


class PolicyNoChasingRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    advisory: bool
    semantics: str
    daily_change_pct_threshold: float | None
    min_trade_value_24h_krw: int | None
    criteria: list[str]
    follow_up: str


class CryptoMarketRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recovery_gate: PolicyRecoveryGate
    support_resistance: PolicySupportResistanceRule
    no_chasing: PolicyNoChasingRule


class PolicyAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    governs: str
    does_not_govern: list[str]


class PosturePolicy(BaseModel):
    """ROB-1106 stage-1 feature gate and five-state shadow contract.

    Only ``shadow`` is accepted in this stage. Later pilot/live modes need
    separate authorization and implementation rather than silently widening
    this schema.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["shadow"]
    states: list[PostureStateName]
    policy_stamp_required: Literal[True]

    @field_validator("states")
    @classmethod
    def validate_exact_five_states(
        cls, value: list[PostureStateName]
    ) -> list[PostureStateName]:
        required = {
            "RESTING",
            "CONDITIONAL",
            "ARMED_DEFERRED",
            "DISARMED",
            "EXPIRED_REARMABLE",
        }
        if len(value) != len(required) or set(value) != required:
            raise ValueError(
                "posture.states must contain exactly the five posture-v1 states"
            )
        return value


class OrderProposalAutoApprovePolicy(BaseModel):
    """Default-off resting-order auto-approval thresholds (ROB-871).

    Caps are denominated in each market's settlement currency: KRW for KR
    equities and crypto, USD for US equities.

    ``breakeven_band_pct`` and ``round_trip_cost_bps`` are the operator-owned
    inputs to the expanded classification (see
    ``order_proposals/auto_approve.py``). They are optional so a deployment
    pinned to an older YAML still loads; the defaults are the same conservative
    values the code floor enforces, and the code floor means a policy edit can
    only ever make the profit-take classification *narrower*, never wider.
    """

    model_config = ConfigDict(extra="forbid")

    min_distance_pct: float = Field(gt=0, le=100)
    per_order_cap: dict[Market, float]
    daily_cap: dict[Market, float]
    breakeven_band_pct: float = Field(default=1.0, gt=0, le=100)
    round_trip_cost_bps: dict[Market, float] = Field(
        default_factory=lambda: {"kr": 47.4, "us": 90.0, "crypto": 10.0}
    )

    @field_validator("per_order_cap", "daily_cap")
    @classmethod
    def validate_market_caps(cls, value: dict[Market, float]) -> dict[Market, float]:
        required = {"kr", "us", "crypto"}
        if set(value) != required:
            raise ValueError(f"market caps must contain exactly {sorted(required)}")
        if any(cap <= 0 for cap in value.values()):
            raise ValueError("market caps must be positive")
        return value

    @field_validator("round_trip_cost_bps")
    @classmethod
    def validate_round_trip_cost(
        cls, value: dict[Market, float]
    ) -> dict[Market, float]:
        required = {"kr", "us", "crypto"}
        if set(value) != required:
            raise ValueError(
                f"round_trip_cost_bps must contain exactly {sorted(required)}"
            )
        if any(bps < 0 for bps in value.values()):
            raise ValueError("round_trip_cost_bps must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_daily_caps(self) -> OrderProposalAutoApprovePolicy:
        if any(
            self.daily_cap[market] < per_order
            for market, per_order in self.per_order_cap.items()
        ):
            raise ValueError("daily cap must be at least the per-order cap")
        return self


class OrderProposalsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve: OrderProposalAutoApprovePolicy


class CrashDayTrigger(BaseModel):
    """ROB-932 — gap-only trigger. Intraday crashes (e.g. 2026-07-13: gap
    -0.8% -> intraday -9.8%) are NOT covered by this trigger; that gap is a
    documented limitation, not an oversight."""

    model_config = ConfigDict(extra="forbid")

    index_symbol: str
    index_gap_pct_max: float


class CrashDayActions(BaseModel):
    """ROB-932 — advisory only, no code enforcement. new_entry_hold applies
    to NEW entries only; averaging-down deep rungs on existing positions are
    exempt (2026-07-16 midday dip-buys measured effective)."""

    model_config = ConfigDict(extra="forbid")

    new_entry_hold: bool
    deep_rung_reprice_to_band_floor: bool
    profit_trim_marketable_allowed: bool
    defensive_brief_cross_check: bool


class CrashDayPolicy(BaseModel):
    """ROB-932 — crash-day advisory playbook. Not enforced in code; a
    cross-check reference for judgment only. defensive_trim execution support
    is out of scope for this PR."""

    model_config = ConfigDict(extra="forbid")

    trigger: CrashDayTrigger
    actions: CrashDayActions


class UserStance(BaseModel):
    """ROB-948 — user investment-stance advisory. Cited by session judgment
    (upside/downside weighting) alongside other advisory context; does not
    override fail-closed risk guards (loss-cut sizing, ladder guards) in
    code. Same advisory-only pattern as ROB-932 crash_day."""

    model_config = ConfigDict(extra="forbid")

    id: str
    stance: str
    implications: list[str]
    risk_scenario: str
    review_condition: str
    review_date: str

    @field_validator("review_date")
    @classmethod
    def validate_review_date_parses(cls, value: str) -> str:
        date.fromisoformat(value)
        return value


class TradingPolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    captured_as_of: str
    source: str
    authority: PolicyAuthority
    posture: PosturePolicy
    order_proposals: OrderProposalsPolicy
    sector_clusters: dict[str, list[str]]
    thresholds: dict[str, PolicyThreshold]
    decision_rules: dict[str, PolicyDecisionRule | SingleShareExitDecisionRule] = Field(
        default_factory=dict
    )
    market_rules: dict[Literal["crypto"], CryptoMarketRules]
    market_overrides: dict[Market, dict[str, ThresholdValue]]
    crash_day: CrashDayPolicy
    user_stances: list[UserStance]
