"""Typed, hash-bound evidence accepted by the funding advisory layer."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.funding_advisory import canonical_decimal

SUPPORTED_MARKETS = frozenset({"crypto", "equity_kr", "equity_us"})
REAL_ACCOUNT_MODES = frozenset({"upbit", "kis_live", "toss_live"})
_EVALUATION_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_evidence_hash(payload_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload_without_hash).encode()).hexdigest()


class NonFundingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    check_id: str = Field(min_length=1, max_length=120)
    check_version: str = Field(min_length=1, max_length=64)
    verdict: Literal["passed"]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def validate_evaluated_at(cls, value: datetime) -> datetime:
        _aware(value, "non_funding_checks.evaluated_at")
        return value


class PassedNonFundingGateEvidence(BaseModel):
    """Proof that every non-cash check passed before funding assessment.

    ``gate_version`` is the gate contract/schema version. It stays stable
    across evaluations of that contract and is distinct from the per-evaluation
    ``evidence_hash``.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner_user_id: int = Field(gt=0)
    source_kind: str = Field(min_length=1, max_length=64)
    source_candidate_id: str = Field(min_length=1, max_length=160)
    gate_name: str = Field(min_length=1, max_length=120)
    gate_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9._-]*$",
    )
    gate_verdict: Literal["passed"]
    gate_evaluated_at: datetime
    valid_until: datetime
    market: Literal["crypto", "equity_kr", "equity_us"]
    target_account_mode: Literal["upbit", "kis_live", "toss_live"]
    broker_account_id: str = Field(min_length=1, max_length=160)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy"]
    order_type: Literal["limit", "market"]
    quantity: str | None = None
    price_reference: str | None = None
    blocking_reasons: list[str] = Field(default_factory=list, max_length=0)
    non_funding_checks: list[NonFundingCheck] = Field(min_length=1)
    upstream_priority: str | None = Field(default=None, max_length=120)
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("gate_evaluated_at", "valid_until")
    @classmethod
    def validate_datetimes(cls, value: datetime) -> datetime:
        _aware(value, "gate datetime")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if _EVALUATION_HASH_RE.fullmatch(self.gate_version):
            raise ValueError(
                "gate_version is a contract/schema version, not an evaluation hash"
            )
        if _aware(self.valid_until, "valid_until") <= _aware(
            self.gate_evaluated_at, "gate_evaluated_at"
        ):
            raise ValueError("valid_until must be after gate_evaluated_at")
        expected = compute_evidence_hash(self.canonical_payload())
        if self.evidence_hash != expected:
            raise ValueError("evidence_hash does not match canonical evidence")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"evidence_hash"})

    @classmethod
    def issue(cls, **payload: Any) -> PassedNonFundingGateEvidence:
        """Issue test/upstream evidence while keeping verification mandatory."""

        normalized = dict(payload)
        normalized["non_funding_checks"] = [
            check
            if isinstance(check, NonFundingCheck)
            else NonFundingCheck.model_validate(check)
            for check in normalized.get("non_funding_checks", [])
        ]
        provisional = cls.model_construct(
            **normalized,
            evidence_hash="0" * 64,
        )
        normalized["evidence_hash"] = compute_evidence_hash(
            provisional.canonical_payload()
        )
        return cls.model_validate(normalized)


class FundingAssessment(BaseModel):
    """Broker-authoritative, read-only funding observation for one candidate."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    required_cash: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    target_buying_power: Decimal = Field(ge=0, max_digits=38, decimal_places=12)
    other_pending_required: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=38, decimal_places=12
    )
    reserved_cash: Decimal = Field(
        default=Decimal("0"), ge=0, max_digits=38, decimal_places=12
    )
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    observed_at: datetime
    valid_until: datetime
    source: str = Field(min_length=1, max_length=160)

    @field_validator("observed_at", "valid_until")
    @classmethod
    def validate_datetimes(cls, value: datetime) -> datetime:
        _aware(value, "funding assessment datetime")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if _aware(self.valid_until, "valid_until") <= _aware(
            self.observed_at, "observed_at"
        ):
            raise ValueError("funding assessment valid_until must be after observed_at")
        return self

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "required_cash": canonical_decimal(self.required_cash),
            "target_buying_power": canonical_decimal(self.target_buying_power),
            "other_pending_required": canonical_decimal(self.other_pending_required),
            "reserved_cash": canonical_decimal(self.reserved_cash),
            "currency": self.currency,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "source": self.source,
        }


class FundingCandidateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: PassedNonFundingGateEvidence
    assessment: FundingAssessment

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.evidence.currency != self.assessment.currency:
            raise ValueError("evidence and funding assessment currency mismatch")
        if _aware(self.assessment.valid_until, "assessment.valid_until") > _aware(
            self.evidence.valid_until, "evidence.valid_until"
        ):
            raise ValueError("funding assessment cannot outlive gate evidence")
        return self


class FundingRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route_id: Literal[
        "EXTERNAL_PARKING_KRW",
        "USD_CONVERSION",
        "CREDIT_LINE_SHORT_TERM",
        "PROFITABLE_TRIM",
        "LOSS_CUT_ROTATION",
    ]
    label: str
    amount_status: Literal["known", "unknown", "conditional"]
    route_fundable_amount: Decimal | None = None
    counted_fundable_amount: Decimal = Decimal("0")
    confidence: Literal[
        "broker_authoritative", "operator_declared", "conditional", "unknown"
    ]
    source_as_of: datetime | None = None
    deadline_status: Literal["met", "missed", "unknown"]
    explicit_cost: Decimal | None = None
    eta_minutes: int | None = Field(default=None, ge=0)
    realized_impact: Decimal | None = None
    reversibility: Literal["reversible", "conditional", "irreversible", "unknown"]
    eligibility: Literal["eligible", "locked", "comparison_unavailable"]
    reason_codes: list[str] = Field(default_factory=list)
    comparison: Literal[
        "preferred", "situation_dependent", "dominated", "unavailable"
    ] = "unavailable"

    def json_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        for field in (
            "route_fundable_amount",
            "counted_fundable_amount",
            "explicit_cost",
            "realized_impact",
        ):
            value = getattr(self, field)
            payload[field] = None if value is None else canonical_decimal(value)
        return payload


__all__ = [
    "FundingAssessment",
    "FundingCandidateEvent",
    "FundingRoute",
    "NonFundingCheck",
    "PassedNonFundingGateEvidence",
    "compute_evidence_hash",
]
