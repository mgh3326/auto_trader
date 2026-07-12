"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


class TradingPolicyDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    captured_as_of: str
    source: str
    authority: PolicyAuthority
    sector_clusters: dict[str, list[str]]
    thresholds: dict[str, PolicyThreshold]
    decision_rules: dict[str, PolicyDecisionRule] = Field(default_factory=dict)
    market_rules: dict[Literal["crypto"], CryptoMarketRules]
    market_overrides: dict[Market, dict[str, ThresholdValue]]
