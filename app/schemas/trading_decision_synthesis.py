"""TradingAgents pre-proposal synthesis transport schemas.

These schemas intentionally model TradingAgents as advisory evidence only. They
contain no broker/order/watch side-effect affordances.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

CandidateSide = Literal["buy", "sell", "none"]
ProposalKindValue = Literal[
    "trim",
    "add",
    "enter",
    "exit",
    "pullback_watch",
    "breakout_watch",
    "avoid",
    "no_action",
    "other",
]


class CandidateAnalysis(BaseModel):
    """Deterministic auto_trader candidate before TradingAgents synthesis."""

    model_config = ConfigDict(extra="allow")

    symbol: str = Field(min_length=1, max_length=32)
    instrument_type: str = Field(min_length=1, max_length=32)
    side: CandidateSide = "none"
    confidence: int = Field(ge=0, le=100)
    proposal_kind: ProposalKindValue = "other"
    rationale: str = Field(default="", max_length=4000)
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = None
    amount: Decimal | None = None
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    threshold_pct: Decimal | None = None
    currency: str | None = None
    deterministic_payload: dict[str, Any] = Field(default_factory=dict)


class AdvisoryEvidence(BaseModel):
    """TradingAgents advisory output normalized for synthesis.

    The two literal invariants make it impossible to accidentally represent a
    TradingAgents result as execution authority.
    """

    model_config = ConfigDict(extra="allow")

    advisory_only: Literal[True] = True
    execution_allowed: Literal[False] = False
    advisory_action: str = Field(min_length=1, max_length=128)
    decision_text: str = Field(default="", max_length=20000)
    final_trade_decision_text: str = Field(default="", max_length=20000)
    provider: str | None = None
    model: str | None = None
    base_url: HttpUrl | str | None = None
    warnings: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    raw_state_keys: list[str] = Field(default_factory=list)
    as_of_date: date | None = None

    @field_validator("warnings", "risk_flags", "raw_state_keys", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    @property
    def normalized_action(self) -> str:
        return self.advisory_action.strip().lower().replace(" ", "_")


class SynthesizedProposal(BaseModel):
    """Final proposal after TradingAgents evidence is reflected."""

    candidate: CandidateAnalysis
    advisory: AdvisoryEvidence
    final_proposal_kind: ProposalKindValue
    final_side: CandidateSide
    final_confidence: int = Field(ge=0, le=100)
    conflict: bool = False
    applied_policies: list[str] = Field(default_factory=list)
    evidence_summary: str = Field(default="", max_length=4000)
    original_payload: dict[str, Any]
    original_rationale: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _payload_preserves_advisory_invariants(self) -> SynthesizedProposal:
        if self.original_payload.get("advisory_only") is not True:
            raise ValueError("original_payload.advisory_only must be True")
        if self.original_payload.get("execution_allowed") is not False:
            raise ValueError("original_payload.execution_allowed must be False")
        synthesis = self.original_payload.get("synthesis")
        if not isinstance(synthesis, dict):
            raise ValueError("original_payload.synthesis must be present")
        return self


def advisory_from_runner_result(payload: dict[str, Any]) -> AdvisoryEvidence:
    """Normalize the ROB-9 TradingAgents runner JSON into advisory evidence."""

    metadata = payload.get("llm", {}) if isinstance(payload.get("llm"), dict) else {}
    config = (
        payload.get("config", {}) if isinstance(payload.get("config"), dict) else {}
    )
    warnings = (
        payload.get("warnings", {}) if isinstance(payload.get("warnings"), dict) else {}
    )
    warning_items: list[str] = []
    for value in warnings.values():
        if isinstance(value, list):
            warning_items.extend(str(item) for item in value)
        elif value:
            warning_items.append(str(value))

    return AdvisoryEvidence(
        advisory_only=payload.get("advisory_only", True),
        execution_allowed=payload.get("execution_allowed", False),
        advisory_action=str(
            payload.get("decision") or payload.get("advisory_action") or "Unknown"
        ),
        decision_text=str(payload.get("decision") or ""),
        final_trade_decision_text=str(payload.get("final_trade_decision") or ""),
        provider=metadata.get("provider") or payload.get("provider"),
        model=metadata.get("model") or config.get("model") or payload.get("model"),
        base_url=metadata.get("base_url")
        or config.get("base_url")
        or payload.get("base_url"),
        warnings=warning_items,
        risk_flags=payload.get("risk_flags") or [],
        raw_state_keys=payload.get("raw_state_keys") or [],
        as_of_date=payload.get("as_of_date"),
    )
