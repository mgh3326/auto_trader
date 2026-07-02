"""Pydantic schema for config/trading_policy.yaml (ROB-646).

The YAML is the single authoritative source of trading judgment thresholds
(seeded verbatim from the ROB-643 playbook policy_keys block). This module
validates its shape; extra="forbid" everywhere so a typo in the operator PR
fails loudly instead of silently dropping a key.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Lane = Literal["buy", "sell", "discovery"]
Market = Literal["kr", "us", "crypto"]

ThresholdValue = int | float | str | list[int | float]


class PolicyThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lanes: list[Lane]
    value: ThresholdValue
    unit: str
    semantics: str
    of: int | None = None


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
    market_overrides: dict[Market, dict[str, ThresholdValue]]
