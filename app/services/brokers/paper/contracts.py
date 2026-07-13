"""Frozen, broker-neutral paper execution contracts for experiment callers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.brokers.capabilities import Broker


class PaperOperation(StrEnum):
    PREVIEW = "preview"
    SUBMIT = "submit"
    CANCEL = "cancel"
    GET_ORDER = "get_order"
    RECONCILE = "reconcile"
    LINK_NATIVE_ORDER = "link_native_order"


class PaperOperationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"


class PaperReasonCode(StrEnum):
    OK = "ok"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    PROVENANCE_VERIFIER_UNAVAILABLE = "provenance_verifier_unavailable"
    PROVENANCE_VERIFICATION_FAILED = "provenance_verification_failed"
    PROVENANCE_EVIDENCE_INVALID = "provenance_evidence_invalid"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must be non-blank")
    return value


def _require_positive_finite(value: Decimal | None) -> Decimal | None:
    if value is not None and (not value.is_finite() or value <= 0):
        raise ValueError("value must be finite and positive")
    return value


class _PaperIntentFields(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    intent_id: str
    experiment_id: str
    run_id: str
    cohort_id: str
    strategy_version_id: str
    strategy_hash: str
    config_hash: str
    policy_hash: str
    venue: Broker
    account_mode: str
    product: str
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    time_in_force: str | None = None
    qty: Decimal | None = None
    notional: Decimal | None = None
    price: Decimal | None = None
    market_snapshot_id: str
    market_snapshot_hash: str
    market_snapshot_as_of: datetime
    market_snapshot_source: str
    source_buy_reference: str | None = None

    @field_validator(
        "intent_id",
        "experiment_id",
        "run_id",
        "cohort_id",
        "strategy_version_id",
        "strategy_hash",
        "config_hash",
        "policy_hash",
        "account_mode",
        "product",
        "symbol",
        "time_in_force",
        "market_snapshot_id",
        "market_snapshot_hash",
        "market_snapshot_source",
        "source_buy_reference",
    )
    @classmethod
    def _non_blank_strings(cls, value: str | None) -> str | None:
        return None if value is None else _require_non_blank(value)

    @field_validator("qty", "notional", "price")
    @classmethod
    def _positive_numbers(cls, value: Decimal | None) -> Decimal | None:
        return _require_positive_finite(value)

    @field_validator("market_snapshot_as_of")
    @classmethod
    def _aware_snapshot_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("market_snapshot_as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _valid_sizing_and_sell_source(self) -> _PaperIntentFields:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        if self.side == "sell" and self.source_buy_reference is None:
            raise ValueError("sell requires source_buy_reference")
        return self


class PaperOrderRequest(_PaperIntentFields):
    """Caller-claimed experiment order; server-owned fields are intentionally absent."""


class VerifiedExperimentProvenance(_PaperIntentFields):
    """Trusted evidence returned by the composition-root provenance verifier."""

    decision_id: str
    reference_price: Decimal
    source_buy_client_order_id: str | None = None

    @field_validator("decision_id", "source_buy_client_order_id")
    @classmethod
    def _non_blank_provenance_strings(cls, value: str | None) -> str | None:
        return None if value is None else _require_non_blank(value)

    @field_validator("reference_price")
    @classmethod
    def _positive_reference_price(cls, value: Decimal) -> Decimal:
        validated = _require_positive_finite(value)
        assert validated is not None
        return validated

    @model_validator(mode="after")
    def _verified_sell_has_native_source(self) -> VerifiedExperimentProvenance:
        if self.side == "sell" and self.source_buy_client_order_id is None:
            raise ValueError("verified sell requires source_buy_client_order_id")
        return self


class VerifiedPaperOrderIntent(_PaperIntentFields):
    """Exact-bound order passed to venue adapters after provenance verification."""

    decision_id: str
    reference_price: Decimal
    source_buy_client_order_id: str | None = None
    origin: Literal["experiment"]
    idempotency_key: str

    @field_validator("decision_id", "source_buy_client_order_id", "idempotency_key")
    @classmethod
    def _non_blank_server_strings(cls, value: str | None) -> str | None:
        return None if value is None else _require_non_blank(value)

    @field_validator("reference_price")
    @classmethod
    def _positive_intent_reference_price(cls, value: Decimal) -> Decimal:
        validated = _require_positive_finite(value)
        assert validated is not None
        return validated

    @model_validator(mode="after")
    def _intent_sell_has_native_source(self) -> VerifiedPaperOrderIntent:
        if self.side == "sell" and self.source_buy_client_order_id is None:
            raise ValueError("verified sell requires source_buy_client_order_id")
        return self


class PaperRiskSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    open_exposure: Decimal | None
    reserved_notional: Decimal | None
    daily_realized_loss: Decimal
    quote_price: Decimal
    spread_bps: Decimal
    data_age_seconds: Decimal
    quote_source: str
    quote_as_of: datetime
    policy_version: str
    policy_hash: str


class PaperOperationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    operation: PaperOperation
    status: PaperOperationStatus
    reason_code: PaperReasonCode | str
    venue: Broker
    native_order_id: str | None = None
    native_client_order_id: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)
    risk_snapshot: PaperRiskSnapshot | None = None
    replayed: bool = False

    @classmethod
    def blocked(
        cls,
        *,
        operation: PaperOperation,
        venue: Broker,
        reason_code: PaperReasonCode | str,
        evidence: dict[str, object] | None = None,
    ) -> PaperOperationResult:
        return cls(
            operation=operation,
            status=PaperOperationStatus.BLOCKED,
            reason_code=reason_code,
            venue=venue,
            evidence={} if evidence is None else evidence,
        )


class ExperimentProvenanceVerifier(Protocol):
    async def verify(
        self, request: PaperOrderRequest
    ) -> VerifiedExperimentProvenance: ...


class PaperBrokerPort(Protocol):
    broker: Broker

    async def preview(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...

    async def submit(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...

    async def cancel(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...

    async def get_order(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...

    async def reconcile(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...

    async def link_native_order(
        self, intent: VerifiedPaperOrderIntent
    ) -> PaperOperationResult: ...


def derive_paper_idempotency_key(
    provenance: VerifiedExperimentProvenance,
) -> str:
    """Derive a provider-bounded key from exact verified immutable evidence."""

    canonical = json.dumps(
        provenance.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"rob845-{digest[:29]}"


__all__ = [
    "ExperimentProvenanceVerifier",
    "PaperBrokerPort",
    "PaperOperation",
    "PaperOperationResult",
    "PaperOperationStatus",
    "PaperOrderRequest",
    "PaperReasonCode",
    "PaperRiskSnapshot",
    "VerifiedExperimentProvenance",
    "VerifiedPaperOrderIntent",
    "derive_paper_idempotency_key",
]
