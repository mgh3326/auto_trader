"""Typed ROB-1115 strategy learning-memory contracts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.research_canonical_hash import encode_canonical

StrategyLearningStage = Literal[
    "discovery",
    "offline",
    "sealed_oos",
    "shadow",
    "paper",
    "live",
    "ops",
]
StrategyLearningVerdict = Literal[
    "promote",
    "iterate",
    "retire",
    "inconclusive",
    "retry_same_identity",
]
StrategyLearningFailureClass = Literal[
    "data_quality",
    "insufficient_evidence",
    "no_signal",
    "gross_edge",
    "cost_turnover",
    "robustness",
    "risk",
    "execution_gap",
    "operational",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_REF_RE = re.compile(r"^(?:[a-z][a-z0-9_]*:)?[0-9a-f]{64}$")


class FailureFingerprint(BaseModel):
    """Searchable deterministic failure identity.

    ``market`` and ``horizon`` are required because they are the two explicit
    dimensions of ``search_failures``. Additional mechanism-specific dimensions
    are retained verbatim and participate in the canonical serialization.
    """

    market: str = Field(min_length=1, max_length=128)
    horizon: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        encode_canonical(self.model_dump(mode="python"))
        return self


class StrategyLearningPayload(BaseModel):
    """Minimum structured learning contract from ROB-1115."""

    tested_claim: Any
    observed: Any
    falsified_claims: list[Any]
    preserved_claims: list[Any]
    next_question: Any
    allowed_change_axis: str = Field(min_length=1)
    prohibited_changes: list[Any]
    stop_rule: Any
    schema_version: str | int

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        required = (
            "tested_claim",
            "observed",
            "next_question",
            "stop_rule",
            "schema_version",
        )
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"learning payload fields cannot be null: {missing}")
        if not self.allowed_change_axis.strip():
            raise ValueError("allowed_change_axis cannot be blank")
        if isinstance(self.schema_version, str) and not self.schema_version.strip():
            raise ValueError("schema_version cannot be blank")
        encode_canonical(self.model_dump(mode="python"))
        return self


class StrategyLearningEventRequest(BaseModel):
    """Append-only write request.

    ``experiment_id=None`` is the explicit unregistered-track state. Supplying
    an id binds the event to an existing ROB-846 experiment through a real FK.
    """

    experiment_id: str | None = None
    stage: StrategyLearningStage
    verdict: StrategyLearningVerdict
    failure_class: StrategyLearningFailureClass
    reason_codes: list[str] = Field(min_length=1)
    evidence_refs: list[str]
    failure_fingerprint: FailureFingerprint
    learning_payload: StrategyLearningPayload
    idempotency_key: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    actor_role: str = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid")

    @field_validator("experiment_id")
    @classmethod
    def _valid_experiment_id(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256_RE.fullmatch(value):
            raise ValueError("experiment_id must be lowercase 64-hex SHA-256")
        return value

    @field_validator("reason_codes")
    @classmethod
    def _valid_reason_codes(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("reason_codes cannot contain blank values")
        return values

    @field_validator("evidence_refs")
    @classmethod
    def _reference_hashes_only(cls, values: list[str]) -> list[str]:
        invalid = [value for value in values if not _EVIDENCE_REF_RE.fullmatch(value)]
        if invalid:
            raise ValueError(
                "evidence_refs accepts only SHA-256 references "
                "(optionally kind-prefixed), never inline evidence"
            )
        return values

    @field_validator("idempotency_key", "actor_id", "actor_role")
    @classmethod
    def _nonblank_audit_fields(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("audit fields cannot be blank")
        return value


class StrategyLearningEventRecord(BaseModel):
    """Decoded typed read model for one immutable event."""

    memory_event_id: str
    experiment_id: str | None
    stage: StrategyLearningStage
    verdict: StrategyLearningVerdict
    failure_class: StrategyLearningFailureClass
    reason_codes: list[str]
    evidence_refs: list[str]
    failure_fingerprint: FailureFingerprint
    learning_payload: StrategyLearningPayload
    idempotency_key: str
    request_hash: str
    actor_id: str
    actor_role: str
    created_at: datetime


def canonical_event_request_payload(
    request: StrategyLearningEventRequest,
) -> dict[str, Any]:
    """All semantic fields, excluding the idempotency lookup key."""
    payload = request.model_dump(mode="python")
    payload.pop("idempotency_key")
    return payload


__all__ = [
    "FailureFingerprint",
    "StrategyLearningEventRecord",
    "StrategyLearningEventRequest",
    "StrategyLearningFailureClass",
    "StrategyLearningPayload",
    "StrategyLearningStage",
    "StrategyLearningVerdict",
    "canonical_event_request_payload",
]
