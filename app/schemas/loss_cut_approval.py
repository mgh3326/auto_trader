from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceStatus = Literal["filled", "stale", "missing", "unavailable", "source_error"]


class LossCutEvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EvidenceStatus
    label: str
    value: dict[str, Any] | None = None
    reason: str | None = None
    source: str | None = None
    as_of: str | None = None
    valid_until: str | None = None


class LossCutPositionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str
    account_mode: str
    market: str
    symbol: str
    total_quantity: str
    sellable_quantity: str | None
    pending_sell_quantity: str | None
    average_price: str | None
    current_price: str | None
    source: str
    source_status: EvidenceStatus
    source_reason: str | None = None
    observed_at: str


class LossCutEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["symbol", "proposal"]
    symbol: str
    proposal_id: str | None = None
    generated_at: str
    can_begin: bool
    positions: list[LossCutPositionEvidence] = Field(default_factory=list)
    loss: LossCutEvidenceField
    reason: LossCutEvidenceField
    r931: LossCutEvidenceField
    consensus: LossCutEvidenceField
    watch: LossCutEvidenceField
    fingerprint: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class LossCutBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LossCutBeginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    ceremony_id: str
    expires_at: str
    evidence: LossCutEvidenceResponse
    fingerprint: dict[str, Any]
    next_step: Literal["confirm"] = "confirm"


class LossCutConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_id: str = Field(min_length=32, max_length=256)


class LossCutConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    status: Literal["validated_no_execution"] = "validated_no_execution"
    evidence: LossCutEvidenceResponse
    fingerprint: dict[str, Any]
