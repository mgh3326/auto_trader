"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Lane = Literal["buy", "sell", "discovery"]
Market = Literal["kr", "us", "crypto"]

ThresholdValue = int | float | str | list[int | float]
RuleConditionValue = int | float | str | bool | list[int | float | str | bool]
PolicyComparison = Literal["gt", "gte", "lt", "lte", "eq"]


class PolicyThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    value: ThresholdValue
    unit: str
    semantics: str
    of: int | None = None


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


class OrderProposalAutoApprovePolicy(BaseModel):
    """Default-off resting-order auto-approval thresholds (ROB-871).

    Caps are denominated in each market's settlement currency: KRW for KR
    equities and crypto, USD for US equities.
    """

    model_config = ConfigDict(extra="forbid")

    min_distance_pct: float = Field(gt=0, le=100)
    per_order_cap: dict[Market, float]
    daily_cap: dict[Market, float]

    @field_validator("per_order_cap", "daily_cap")
    @classmethod
    def validate_market_caps(cls, value: dict[Market, float]) -> dict[Market, float]:
        required = {"kr", "us", "crypto"}
        if set(value) != required:
            raise ValueError(f"market caps must contain exactly {sorted(required)}")
        if any(cap <= 0 for cap in value.values()):
            raise ValueError("market caps must be positive")
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
    order_proposals: OrderProposalsPolicy
    sector_clusters: dict[str, list[str]]
    thresholds: dict[str, PolicyThreshold]
    decision_rules: dict[str, PolicyDecisionRule] = Field(default_factory=dict)
    market_rules: dict[Literal["crypto"], CryptoMarketRules]
    market_overrides: dict[Market, dict[str, ThresholdValue]]
    crash_day: CrashDayPolicy
    user_stances: list[UserStance]
