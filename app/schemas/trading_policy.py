"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

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
