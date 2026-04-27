from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TradingAgentsLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    base_url: str


class TradingAgentsConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_debate_rounds: int
    max_risk_discuss_rounds: int
    max_recur_limit: int
    output_language: str
    checkpoint_enabled: bool


class TradingAgentsWarnings(BaseModel):
    model_config = ConfigDict(extra="allow")

    structured_output: list[str] = Field(default_factory=list)


class TradingAgentsRunnerResult(BaseModel):
    """Strict contract for advisory-only TradingAgents runner output."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["ok"]
    symbol: str = Field(min_length=1, max_length=64)
    as_of_date: date
    decision: str
    advisory_only: Literal[True]
    execution_allowed: Literal[False]
    analysts: list[str] = Field(min_length=1)
    llm: TradingAgentsLLM
    config: TradingAgentsConfigSnapshot
    warnings: TradingAgentsWarnings
    final_trade_decision: str
    raw_state_keys: list[str]
