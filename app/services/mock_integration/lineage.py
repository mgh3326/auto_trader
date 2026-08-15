"""Server-generated lineage envelopes for mock/paper/demo execution.

This module is deliberately pure: it creates immutable records, derives
deterministic identifiers, and defines a persistence port.  It does not
select a lane profile, persist a record, or call a broker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)

from app.schemas.execution_contracts import (
    DecisionIntent,
    ExecutionPlan,
    OrderAttempt,
)
from app.services.brokers.client_order_ids import (
    BROKER_CLIENT_ID_CONSTRAINT_VIOLATION,
    BrokerClientIdTarget,
    assert_broker_client_order_id,
)

_INTENT_V1_DOMAIN: Final[str] = "mock-intent-v1:"
_PLAN_V1_DOMAIN: Final[str] = "mock-plan-v1:"
_ATTEMPT_V1_DOMAIN: Final[str] = "mock-attempt-v1:"
_IDEMPOTENCY_V1_DOMAIN: Final[str] = "mock-idempotency-v1:"

# These tuples are a compatibility boundary, not a convenience list.  Add a
# new domain version rather than changing either v1 input set.
_INTENT_V1_HASH_FIELDS: Final[tuple[str, ...]] = (
    "policy_version",
    "policy_version_hash",
    "decision_timestamp",
    "market_data_cutoff",
    "symbol",
    "side",
    "target_notional",
    "target_notional_currency",
    "limit_policy",
    "expiry_policy",
    "rationale",
)
_PLAN_V1_HASH_FIELDS: Final[tuple[str, ...]] = (
    "decision_intent_id",
    "lane_id",
    "broker",
    "account_profile",
    "account_mode",
    "normalized_symbol",
    "quantity",
    "limit_price",
    "quote_currency",
    "tick_rounding",
    "session",
    "time_in_force",
    "min_order_validation",
    "risk_caps",
)


class LineageReasonCode(StrEnum):
    """Stable reason-code vocabulary for downstream aggregation."""

    LINEAGE_PERSISTENCE_UNAVAILABLE = "lineage_persistence_unavailable"
    CURRENCY_CONVERSION_NOT_AUTHORIZED = "currency_conversion_not_authorized"
    LANE_QUOTE_CURRENCY_MISMATCH = "lane_quote_currency_mismatch"
    BROKER_CLIENT_ID_CONSTRAINT_VIOLATION = BROKER_CLIENT_ID_CONSTRAINT_VIOLATION


LINEAGE_REASON_CODES: Final[frozenset[str]] = frozenset(
    reason.value for reason in LineageReasonCode
)


class CallerOwnedIdRejected(ValueError):
    """A caller attempted to bypass the server-generated ID boundary."""


class HashVersionUpgradeRequired(ValueError):
    """A v1 model shape changed and must move to a new hash domain."""


class LineagePersistenceUnavailable(RuntimeError):
    """No future-owned persistence implementation was supplied."""

    reason_code: Final[str] = LineageReasonCode.LINEAGE_PERSISTENCE_UNAVAILABLE

    def __init__(self) -> None:
        super().__init__(self.reason_code)


LineageNonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_Currency = Literal["KRW", "USD", "USDT"]


class _LineageDraft(BaseModel):
    """Strict server-factory input with no caller-owned identifier fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DecisionIntentDraft(_LineageDraft):
    policy_version: LineageNonBlank
    policy_version_hash: LineageNonBlank
    decision_timestamp: datetime
    market_data_cutoff: datetime
    symbol: LineageNonBlank
    side: Literal["buy", "sell"]
    target_notional: Decimal = Field(gt=0, allow_inf_nan=False)
    target_notional_currency: _Currency
    limit_policy: dict[str, Any]
    expiry_policy: dict[str, Any]
    rationale: LineageNonBlank


class ExecutionPlanDraft(_LineageDraft):
    lane_id: LineageNonBlank
    broker: LineageNonBlank
    account_profile: LineageNonBlank
    account_mode: LineageNonBlank
    normalized_symbol: LineageNonBlank
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    limit_price: Decimal | None
    quote_currency: _Currency
    tick_rounding: dict[str, Any]
    session: LineageNonBlank | None
    time_in_force: LineageNonBlank | None
    min_order_validation: dict[str, Any]
    risk_caps: dict[str, Any]


class OrderAttemptDraft(_LineageDraft):
    cycle_id: LineageNonBlank
    attempt_seq: int = Field(ge=1)
    lane_prefix: LineageNonBlank
    broker_client_id_target: BrokerClientIdTarget


class LineageEnvelope(_LineageDraft):
    """Immutable one-plan lineage snapshot; mirror lanes receive separate ones."""

    decision_intent: DecisionIntent
    execution_plan: ExecutionPlan | None = None
    order_attempt: OrderAttempt | None = None
    attempt_seq: int | None = Field(default=None, ge=1)
    lane_prefix: LineageNonBlank | None = None
    broker_client_id_target: BrokerClientIdTarget | None = None

    @model_validator(mode="after")
    def _validate_containment(self) -> LineageEnvelope:
        if self.decision_intent.decision_intent_id != derive_intent_v1_id(
            self.decision_intent
        ):
            raise CallerOwnedIdRejected("decision_intent_id must be server-generated")
        if (
            self.execution_plan is not None
            and self.execution_plan.decision_intent_id
            != self.decision_intent.decision_intent_id
        ):
            raise ValueError("execution plan does not belong to decision intent")
        if (
            self.execution_plan is not None
            and self.execution_plan.execution_plan_id
            != derive_plan_v1_id(self.execution_plan)
        ):
            raise CallerOwnedIdRejected("execution_plan_id must be server-generated")
        if self.order_attempt is not None:
            if self.execution_plan is None:
                raise ValueError("order attempt requires an execution plan")
            if (
                self.attempt_seq is None
                or self.lane_prefix is None
                or self.broker_client_id_target is None
            ):
                raise CallerOwnedIdRejected(
                    "order attempt correlation metadata must be factory-generated"
                )
            if (
                self.order_attempt.execution_plan_id
                != self.execution_plan.execution_plan_id
            ):
                raise ValueError("order attempt does not belong to execution plan")
            expected_attempt_id = derive_attempt_v1_id(
                execution_plan_id=self.execution_plan.execution_plan_id,
                cycle_id=self.order_attempt.cycle_id,
                attempt_seq=self.attempt_seq,
            )
            if self.order_attempt.order_attempt_id != expected_attempt_id:
                raise CallerOwnedIdRejected("order_attempt_id must be server-generated")
            expected_idempotency_key = derive_idempotency_key(
                execution_plan_id=self.execution_plan.execution_plan_id,
                cycle_id=self.order_attempt.cycle_id,
            )
            if self.order_attempt.idempotency_key != expected_idempotency_key:
                raise CallerOwnedIdRejected("idempotency_key must be server-generated")
            expected_broker_client_order_id = derive_broker_client_order_id(
                execution_plan_id=self.execution_plan.execution_plan_id,
                cycle_id=self.order_attempt.cycle_id,
                lane_prefix=self.lane_prefix,
                target=self.broker_client_id_target,
            )
            if (
                self.order_attempt.broker_client_order_id
                != expected_broker_client_order_id
            ):
                raise CallerOwnedIdRejected(
                    "broker_client_order_id must be server-generated"
                )
        elif any(
            value is not None
            for value in (
                self.attempt_seq,
                self.lane_prefix,
                self.broker_client_id_target,
            )
        ):
            raise ValueError("attempt metadata requires an order attempt")
        return self


@runtime_checkable
class LineagePersistencePort(Protocol):
    """Future lane-owned write boundary; no implementation lives in J2B."""

    async def persist(self, envelope: LineageEnvelope, /) -> None:
        """Persist an immutable lineage envelope in a later lane."""


def require_lineage_persistence_port(
    port: LineagePersistencePort | None, /
) -> LineagePersistencePort:
    """Fail closed when a later lane has not supplied its persistence port."""

    if port is None or not isinstance(port, LineagePersistencePort):
        raise LineagePersistenceUnavailable()
    return port


class _HashProjection(RootModel[dict[str, Any]]):
    """A JSON-ready projection used solely as canonical hash input."""

    model_config = ConfigDict(frozen=True, strict=True)


def normalize_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _normalize_decimal_values(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, dict):
        return {key: _normalize_decimal_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_decimal_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_decimal_values(item) for item in value)
    return value


def canonical_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_frozen_v1_shape(
    model: BaseModel,
    *,
    identifier_field: str,
    hash_fields: tuple[str, ...],
    next_domain: str,
) -> None:
    actual_fields = frozenset(type(model).model_fields)
    expected_fields = frozenset((identifier_field, *hash_fields))
    if actual_fields != expected_fields:
        raise HashVersionUpgradeRequired(
            f"v1 hash inputs changed; use {next_domain} for the revised model"
        )


def _projection_for(model: BaseModel, hash_fields: tuple[str, ...]) -> _HashProjection:
    source = model.model_dump(mode="python")
    return _HashProjection(
        root=_normalize_decimal_values({field: source[field] for field in hash_fields})
    )


def _digest_projection(projection: _HashProjection) -> str:
    return hashlib.sha256(canonical_bytes(projection)).hexdigest()


def _digest_values(values: dict[str, Any]) -> str:
    return _digest_projection(_HashProjection(root=_normalize_decimal_values(values)))


def canonical_intent_v1_bytes(intent: DecisionIntent) -> bytes:
    """Canonical v1 intent payload after the frozen-field projection."""

    _require_frozen_v1_shape(
        intent,
        identifier_field="decision_intent_id",
        hash_fields=_INTENT_V1_HASH_FIELDS,
        next_domain="mock-intent-v2:",
    )
    return canonical_bytes(_projection_for(intent, _INTENT_V1_HASH_FIELDS))


def canonical_plan_v1_bytes(plan: ExecutionPlan) -> bytes:
    """Canonical v1 plan payload after the frozen-field projection."""

    _require_frozen_v1_shape(
        plan,
        identifier_field="execution_plan_id",
        hash_fields=_PLAN_V1_HASH_FIELDS,
        next_domain="mock-plan-v2:",
    )
    return canonical_bytes(_projection_for(plan, _PLAN_V1_HASH_FIELDS))


def derive_intent_v1_id(intent: DecisionIntent) -> str:
    """Derive the full internal stable ID for a v1 decision intent."""

    return f"{_INTENT_V1_DOMAIN}{hashlib.sha256(canonical_intent_v1_bytes(intent)).hexdigest()}"


def derive_plan_v1_id(plan: ExecutionPlan) -> str:
    """Derive the full internal stable ID for a v1 execution plan."""

    return (
        f"{_PLAN_V1_DOMAIN}{hashlib.sha256(canonical_plan_v1_bytes(plan)).hexdigest()}"
    )


def derive_idempotency_key(*, execution_plan_id: str, cycle_id: str) -> str:
    """Derive retry-stable idempotency without an attempt sequence."""

    digest = _digest_values(
        {
            "execution_plan_id": execution_plan_id,
            "cycle_id": cycle_id,
        }
    )
    return f"{_IDEMPOTENCY_V1_DOMAIN}{digest}"


def derive_attempt_v1_id(
    *, execution_plan_id: str, cycle_id: str, attempt_seq: int
) -> str:
    """Derive one unique internal attempt ID for each retry sequence."""

    if (
        isinstance(attempt_seq, bool)
        or not isinstance(attempt_seq, int)
        or attempt_seq < 1
    ):
        raise ValueError("attempt_seq must be a positive integer")
    digest = _digest_values(
        {
            "execution_plan_id": execution_plan_id,
            "cycle_id": cycle_id,
            "attempt_seq": attempt_seq,
        }
    )
    return f"{_ATTEMPT_V1_DOMAIN}{digest}"


def derive_broker_client_order_id(
    *,
    execution_plan_id: str,
    cycle_id: str,
    lane_prefix: str,
    target: BrokerClientIdTarget,
) -> str:
    """Create a bounded broker ID from the retry-stable internal digest."""

    digest = _digest_values(
        {
            "execution_plan_id": execution_plan_id,
            "cycle_id": cycle_id,
        }
    )
    broker_client_id = f"{lane_prefix}-{digest[:24]}"
    assert_broker_client_order_id(target=target, client_order_id=broker_client_id)
    return broker_client_id


def _require_exact_draft(value: object, expected_type: type[_LineageDraft]) -> None:
    if type(value) is not expected_type:
        raise CallerOwnedIdRejected("factory accepts only its ID-free draft type")


class MockLineageFactory:
    """The only construction surface that issues J2B lineage identifiers."""

    def create_decision_intent(self, draft: DecisionIntentDraft, /) -> DecisionIntent:
        _require_exact_draft(draft, DecisionIntentDraft)
        provisional = DecisionIntent(
            decision_intent_id="server-generated",
            **draft.model_dump(mode="python"),
        )
        return provisional.model_copy(
            update={"decision_intent_id": derive_intent_v1_id(provisional)}
        )

    def create_execution_plan(
        self, decision_intent: DecisionIntent, draft: ExecutionPlanDraft, /
    ) -> ExecutionPlan:
        _require_exact_draft(draft, ExecutionPlanDraft)
        self._require_server_intent(decision_intent)
        if decision_intent.target_notional_currency != draft.quote_currency:
            raise ValueError(LineageReasonCode.CURRENCY_CONVERSION_NOT_AUTHORIZED)
        provisional = ExecutionPlan(
            execution_plan_id="server-generated",
            decision_intent_id=decision_intent.decision_intent_id,
            **draft.model_dump(mode="python"),
        )
        return provisional.model_copy(
            update={"execution_plan_id": derive_plan_v1_id(provisional)}
        )

    def create_order_attempt(
        self, envelope: LineageEnvelope, draft: OrderAttemptDraft, /
    ) -> OrderAttempt:
        _require_exact_draft(draft, OrderAttemptDraft)
        if type(envelope) is not LineageEnvelope:
            raise CallerOwnedIdRejected(
                "factory accepts only an immutable lineage envelope"
            )
        if envelope.execution_plan is None:
            raise ValueError("order attempt requires a plan envelope")
        execution_plan = envelope.execution_plan
        self._require_server_plan(execution_plan)
        idempotency_key = derive_idempotency_key(
            execution_plan_id=execution_plan.execution_plan_id,
            cycle_id=draft.cycle_id,
        )
        return OrderAttempt(
            order_attempt_id=derive_attempt_v1_id(
                execution_plan_id=execution_plan.execution_plan_id,
                cycle_id=draft.cycle_id,
                attempt_seq=draft.attempt_seq,
            ),
            execution_plan_id=execution_plan.execution_plan_id,
            cycle_id=draft.cycle_id,
            idempotency_key=idempotency_key,
            broker_client_order_id=derive_broker_client_order_id(
                execution_plan_id=execution_plan.execution_plan_id,
                cycle_id=draft.cycle_id,
                lane_prefix=draft.lane_prefix,
                target=draft.broker_client_id_target,
            ),
            broker_order_id=None,
        )

    def create_intent_envelope(self, draft: DecisionIntentDraft, /) -> LineageEnvelope:
        return LineageEnvelope(decision_intent=self.create_decision_intent(draft))

    def create_plan_envelope(
        self, decision_intent: DecisionIntent, draft: ExecutionPlanDraft, /
    ) -> LineageEnvelope:
        return LineageEnvelope(
            decision_intent=decision_intent,
            execution_plan=self.create_execution_plan(decision_intent, draft),
        )

    def create_attempt_envelope(
        self, envelope: LineageEnvelope, draft: OrderAttemptDraft, /
    ) -> LineageEnvelope:
        if type(envelope) is not LineageEnvelope:
            raise CallerOwnedIdRejected(
                "factory accepts only an immutable lineage envelope"
            )
        if envelope.execution_plan is None:
            raise ValueError("order attempt requires a plan envelope")
        if envelope.order_attempt is not None:
            raise ValueError("plan envelope already contains an order attempt")
        return LineageEnvelope(
            decision_intent=envelope.decision_intent,
            execution_plan=envelope.execution_plan,
            order_attempt=self.create_order_attempt(envelope, draft),
            attempt_seq=draft.attempt_seq,
            lane_prefix=draft.lane_prefix,
            broker_client_id_target=draft.broker_client_id_target,
        )

    @staticmethod
    def _require_server_intent(intent: DecisionIntent) -> None:
        if intent.decision_intent_id != derive_intent_v1_id(intent):
            raise CallerOwnedIdRejected("decision_intent_id must be server-generated")

    @staticmethod
    def _require_server_plan(plan: ExecutionPlan) -> None:
        if plan.execution_plan_id != derive_plan_v1_id(plan):
            raise CallerOwnedIdRejected("execution_plan_id must be server-generated")


__all__ = [
    "CallerOwnedIdRejected",
    "DecisionIntentDraft",
    "ExecutionPlanDraft",
    "HashVersionUpgradeRequired",
    "LINEAGE_REASON_CODES",
    "LineageEnvelope",
    "LineagePersistencePort",
    "LineagePersistenceUnavailable",
    "LineageReasonCode",
    "MockLineageFactory",
    "OrderAttemptDraft",
    "_INTENT_V1_HASH_FIELDS",
    "_PLAN_V1_HASH_FIELDS",
    "canonical_bytes",
    "canonical_intent_v1_bytes",
    "canonical_plan_v1_bytes",
    "derive_attempt_v1_id",
    "derive_broker_client_order_id",
    "derive_idempotency_key",
    "derive_intent_v1_id",
    "derive_plan_v1_id",
    "normalize_decimal",
    "require_lineage_persistence_port",
]
