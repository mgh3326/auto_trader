"""Server-generated lineage envelopes for mock/paper/demo execution.

This module is deliberately pure: it creates immutable records, derives
deterministic identifiers, and defines a persistence port.  It does not
select a lane profile, persist a record, or call a broker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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
    BROKER_CLIENT_ID_TARGET_PLAN_BROKERS,
    BrokerClientIdTarget,
    assert_broker_client_order_id,
)

_INTENT_V1_DOMAIN: Final[str] = "mock-intent-v1:"
_PLAN_V1_DOMAIN: Final[str] = "mock-plan-v1:"
_ATTEMPT_V1_DOMAIN: Final[str] = "mock-attempt-v1:"
_IDEMPOTENCY_V1_DOMAIN: Final[str] = "mock-idempotency-v1:"
BROKER_CLIENT_ID_TARGET_MISMATCH: Final[str] = "broker_client_id_target_mismatch"
BROKER_ORDER_ID_CONFLICT: Final[str] = "broker_order_id_conflict"
# This is derived from the confirmed target map, not a broker-registry allowlist.
_BROKERS_REQUIRING_NATIVE_CLIENT_ID: Final[frozenset[str]] = frozenset(
    BROKER_CLIENT_ID_TARGET_PLAN_BROKERS.values()
)

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
    BROKER_CLIENT_ID_TARGET_MISMATCH = BROKER_CLIENT_ID_TARGET_MISMATCH
    BROKER_ORDER_ID_CONFLICT = BROKER_ORDER_ID_CONFLICT


LINEAGE_REASON_CODES: Final[frozenset[str]] = frozenset(
    reason.value for reason in LineageReasonCode
)


class CallerOwnedIdRejected(ValueError):
    """A caller attempted to bypass the server-generated ID boundary."""


class BrokerClientIdTargetMismatch(ValueError):
    """A client-ID target does not belong to the execution-plan broker."""

    reason_code: Final[str] = BROKER_CLIENT_ID_TARGET_MISMATCH

    def __init__(self) -> None:
        super().__init__(self.reason_code)


class BrokerOrderIdConflict(ValueError):
    """A later acknowledgement conflicts with the immutable broker ID."""

    reason_code: Final[str] = BROKER_ORDER_ID_CONFLICT

    def __init__(self) -> None:
        super().__init__(self.reason_code)


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
    lane_prefix: LineageNonBlank | None
    broker_client_id_target: BrokerClientIdTarget | None

    @model_validator(mode="after")
    def _require_broker_client_id_pair(self) -> OrderAttemptDraft:
        if (self.lane_prefix is None) != (self.broker_client_id_target is None):
            raise ValueError(
                "lane_prefix and broker_client_id_target must be both set or both None"
            )
        return self


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
            if self.attempt_seq is None:
                raise CallerOwnedIdRejected(
                    "order attempt correlation metadata must be factory-generated"
                )
            if (self.lane_prefix is None) != (self.broker_client_id_target is None):
                raise CallerOwnedIdRejected(
                    "broker client ID metadata must be both set or both None"
                )
            _require_broker_client_id_target_matches_plan(
                self.execution_plan,
                self.broker_client_id_target,
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
            if self.order_attempt.broker_order_id is not None:
                normalized_broker_order_id = _normalize_broker_order_id(
                    self.order_attempt.broker_order_id
                )
                if self.order_attempt.broker_order_id != normalized_broker_order_id:
                    raise CallerOwnedIdRejected(
                        "broker_order_id must be strip-normalized"
                    )
            if self.lane_prefix is None:
                if self.order_attempt.broker_client_order_id is not None:
                    raise CallerOwnedIdRejected(
                        "broker_client_order_id must be absent without a native client ID"
                    )
            else:
                assert self.broker_client_id_target is not None
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


def normalize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalize_decimal_values(value: Any) -> Any:
    if isinstance(value, Decimal):
        return normalize_decimal(value)
    if isinstance(value, datetime):
        return normalize_datetime(value)
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


def _require_broker_client_id_target_matches_plan(
    plan: ExecutionPlan,
    target: BrokerClientIdTarget | None,
) -> None:
    if target is None:
        if plan.broker in _BROKERS_REQUIRING_NATIVE_CLIENT_ID:
            raise BrokerClientIdTargetMismatch()
        return
    if BROKER_CLIENT_ID_TARGET_PLAN_BROKERS[target] != plan.broker:
        raise BrokerClientIdTargetMismatch()


def _normalize_broker_order_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("broker_order_id must be a non-empty string")
    normalized_broker_order_id = value.strip()
    if not normalized_broker_order_id:
        raise ValueError("broker_order_id must be a non-empty string")
    return normalized_broker_order_id


def _revalidate_lineage_envelope(envelope: LineageEnvelope) -> LineageEnvelope:
    """Reconstruct an envelope so containment validators run on every ACK."""

    return LineageEnvelope(
        decision_intent=envelope.decision_intent,
        execution_plan=envelope.execution_plan,
        order_attempt=envelope.order_attempt,
        attempt_seq=envelope.attempt_seq,
        lane_prefix=envelope.lane_prefix,
        broker_client_id_target=envelope.broker_client_id_target,
    )


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
        _require_broker_client_id_target_matches_plan(
            execution_plan,
            draft.broker_client_id_target,
        )
        idempotency_key = derive_idempotency_key(
            execution_plan_id=execution_plan.execution_plan_id,
            cycle_id=draft.cycle_id,
        )
        broker_client_order_id: str | None = None
        if draft.lane_prefix is not None:
            assert draft.broker_client_id_target is not None
            broker_client_order_id = derive_broker_client_order_id(
                execution_plan_id=execution_plan.execution_plan_id,
                cycle_id=draft.cycle_id,
                lane_prefix=draft.lane_prefix,
                target=draft.broker_client_id_target,
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
            broker_client_order_id=broker_client_order_id,
            broker_order_id=None,
        )

    def acknowledge_order_attempt(
        self, envelope: LineageEnvelope, broker_order_id: str
    ) -> LineageEnvelope:
        """Attach one broker-supplied order ID without parsing native responses."""

        if type(envelope) is not LineageEnvelope:
            raise CallerOwnedIdRejected(
                "factory accepts only an immutable lineage envelope"
            )
        validated_envelope = _revalidate_lineage_envelope(envelope)
        if (
            validated_envelope.order_attempt is None
            or validated_envelope.execution_plan is None
        ):
            raise ValueError("acknowledgement requires an order attempt envelope")
        _require_broker_client_id_target_matches_plan(
            validated_envelope.execution_plan,
            validated_envelope.broker_client_id_target,
        )
        normalized_broker_order_id = _normalize_broker_order_id(broker_order_id)
        existing_broker_order_id = validated_envelope.order_attempt.broker_order_id
        if existing_broker_order_id is not None:
            if existing_broker_order_id != _normalize_broker_order_id(
                existing_broker_order_id
            ):
                raise CallerOwnedIdRejected("broker_order_id must be strip-normalized")
            if existing_broker_order_id == normalized_broker_order_id:
                return envelope
            raise BrokerOrderIdConflict()
        acknowledged_attempt = validated_envelope.order_attempt.model_copy(
            update={"broker_order_id": normalized_broker_order_id}
        )
        return LineageEnvelope(
            decision_intent=validated_envelope.decision_intent,
            execution_plan=validated_envelope.execution_plan,
            order_attempt=acknowledged_attempt,
            attempt_seq=validated_envelope.attempt_seq,
            lane_prefix=validated_envelope.lane_prefix,
            broker_client_id_target=validated_envelope.broker_client_id_target,
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
    "BROKER_CLIENT_ID_TARGET_MISMATCH",
    "BROKER_ORDER_ID_CONFLICT",
    "BrokerClientIdTargetMismatch",
    "BrokerOrderIdConflict",
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
    "normalize_datetime",
    "require_lineage_persistence_port",
]
