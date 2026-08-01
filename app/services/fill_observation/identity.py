"""Deterministic identity and canonicalization for broker fill evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.symbol import to_db_symbol
from app.models.trading import InstrumentType
from app.services.fill_observation.contracts import (
    BrokerFillEvidence,
    FillObservationIdentity,
    NormalizedBrokerFillEvidence,
    NormalizedFillSettlement,
)
from app.services.fill_observation.errors import InvalidFillEvidence

_IDENTITY_SCHEMA = "fill_observation_identity.v1"
_FILL_FACT_SCHEMA = "fill_observation_fill_fact.v1"
_SETTLEMENT_SCHEMA = "fill_observation_settlement.v1"
_ORDER_SCOPE_SCHEMA = "fill_observation_order_scope.v1"
_PARTITION_SCHEMA = "fill_projection_partition.v1"
_DELIVERY_SCHEMA = "fill_projection_delivery.v1"
_PROJECTION_LOCK_SCHEMA = "fill_projection_lock.v1"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _stable_signed_int64(payload: dict[str, Any]) -> int:
    digest = hashlib.sha256(_canonical_bytes(payload)).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _required_text(value: object, *, field: str, lower: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidFillEvidence(f"{field} must not be blank")
    return normalized.lower() if lower else normalized


def _optional_text(value: object | None, *, field: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        raise InvalidFillEvidence(f"{field} must not be blank when supplied")
    return normalized


def _decimal(
    value: Decimal | int | str | None,
    *,
    field: str,
    strictly_positive: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidFillEvidence(f"{field} must be a finite decimal") from exc
    if not normalized.is_finite():
        raise InvalidFillEvidence(f"{field} must be a finite decimal")
    if strictly_positive and normalized <= 0:
        raise InvalidFillEvidence(f"{field} must be greater than zero")
    if not strictly_positive and normalized < 0:
        raise InvalidFillEvidence(f"{field} must not be negative")
    return normalized


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _aware_datetime(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidFillEvidence(f"{field} must be timezone-aware")
    return value


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def normalize_fill_evidence(
    evidence: BrokerFillEvidence,
) -> NormalizedBrokerFillEvidence:
    """Normalize only representation, never infer broker fill facts."""
    try:
        instrument_type = InstrumentType(str(evidence.instrument_type))
    except ValueError as exc:
        raise InvalidFillEvidence("instrument_type is not supported") from exc

    symbol = _required_text(evidence.symbol, field="symbol")
    if instrument_type is InstrumentType.equity_us:
        # US tickers are canonically dot-separated upper case (``BRK.B``).
        # Case drift between two polls of the same order is representation, not
        # a different fill fact. Only this branch touches the symbol, so crypto
        # (``KRW-BTC``), KR, forex, and index symbols keep their exact meaning.
        symbol = to_db_symbol(symbol).upper()

    side = _required_text(evidence.side, field="side", lower=True)
    if side not in {"buy", "sell"}:
        raise InvalidFillEvidence("side must be buy or sell")

    sequence = _optional_text(
        evidence.broker_fill_sequence,
        field="broker_fill_sequence",
    )
    cumulative_quantity = _decimal(
        evidence.cumulative_quantity,
        field="cumulative_quantity",
    )
    fill_quantity = _decimal(evidence.fill_quantity, field="fill_quantity")
    if sequence is None and cumulative_quantity is None:
        raise InvalidFillEvidence(
            "broker_fill_sequence or cumulative_quantity is required"
        )

    return NormalizedBrokerFillEvidence(
        broker=_required_text(evidence.broker, field="broker", lower=True),
        account_ref=_required_text(evidence.account_ref, field="account_ref"),
        account_mode=_required_text(
            evidence.account_mode,
            field="account_mode",
            lower=True,
        ),
        venue=_required_text(evidence.venue, field="venue", lower=True),
        order_id=_required_text(evidence.order_id, field="order_id"),
        instrument_type=instrument_type,
        symbol=symbol,
        side=side,
        currency=_required_text(evidence.currency, field="currency").upper(),
        evidence_source=_required_text(
            evidence.evidence_source,
            field="evidence_source",
            lower=True,
        ),
        evidence_ref=_required_text(evidence.evidence_ref, field="evidence_ref"),
        observed_at=_aware_datetime(
            evidence.observed_at,
            field="observed_at",
        ),
        broker_fill_sequence=sequence,
        cumulative_quantity=cumulative_quantity,
        fill_quantity=fill_quantity,
        average_price=_decimal(
            evidence.average_price,
            field="average_price",
            strictly_positive=True,
        ),
        last_fill_price=_decimal(
            evidence.last_fill_price,
            field="last_fill_price",
            strictly_positive=True,
        ),
        cumulative_notional=_decimal(
            evidence.cumulative_notional,
            field="cumulative_notional",
        ),
        fee_total=_decimal(evidence.fee_total, field="fee_total"),
        filled_at=_aware_datetime(evidence.filled_at, field="filled_at"),
        correlation_id=_optional_text(
            evidence.correlation_id,
            field="correlation_id",
        ),
    )


def has_positive_fill(evidence: NormalizedBrokerFillEvidence) -> bool:
    """Return whether the typed facts prove any positive filled quantity."""
    return bool(
        (evidence.cumulative_quantity or Decimal(0)) > 0
        or (evidence.fill_quantity or Decimal(0)) > 0
    )


def derive_fill_observation_identity(
    evidence: NormalizedBrokerFillEvidence,
) -> FillObservationIdentity:
    """Derive stable identity, fill-fact fingerprint, settlement, and locks.

    ``fill_fact_hash`` covers only the stable broker fill fact: the order scope,
    the instrument facts, and exactly the quantity the identity is keyed on. A
    contradiction there is a real conflict and stays fail-closed with zero write.

    Everything a provider legitimately revises after the fill — fees, average or
    last price, notional, settled ``filled_at``, and the quantity field the
    identity is *not* keyed on — is carried in a separate settlement payload with
    its own hash. Those never enter ``fill_fact_hash``:

    - Under sequence identity the same fill is re-observed later while the
      order's cumulative quantity has legitimately grown.
    - Under cumulative identity the per-poll reported increment is a snapshot of
      that poll, not a property of the cumulative state.
    """
    if evidence.broker_fill_sequence is not None:
        identity_kind = "broker_fill_sequence"
        identity_value = evidence.broker_fill_sequence
    else:
        identity_kind = "cumulative_quantity"
        identity_value = _decimal_text(evidence.cumulative_quantity)

    order_scope = {
        "schema": _ORDER_SCOPE_SCHEMA,
        "broker": evidence.broker,
        "account_ref": evidence.account_ref,
        "account_mode": evidence.account_mode,
        "venue": evidence.venue,
        "order_id": evidence.order_id,
    }
    observation_identity = _sha256(
        {
            "schema": _IDENTITY_SCHEMA,
            "order_scope": order_scope,
            "identity_kind": identity_kind,
            "identity_value": identity_value,
        }
    )
    fill_fact: dict[str, Any] = {
        "schema": _FILL_FACT_SCHEMA,
        "order_scope": order_scope,
        "instrument_type": evidence.instrument_type.value,
        "symbol": evidence.symbol,
        "side": evidence.side,
        "currency": evidence.currency,
        "identity_kind": identity_kind,
        "identity_value": identity_value,
    }
    if identity_kind == "broker_fill_sequence":
        # The reported quantity is that sequence's own delta, so it is a stable
        # fact. Under cumulative identity it is a per-poll snapshot instead.
        fill_fact["fill_quantity"] = _decimal_text(evidence.fill_quantity)
    fill_fact_hash = _sha256(fill_fact)
    partition_key = _sha256(
        {
            "schema": _PARTITION_SCHEMA,
            "order_scope": order_scope,
        }
    )
    return FillObservationIdentity(
        value=observation_identity,
        kind=identity_kind,
        fill_fact_hash=fill_fact_hash,
        settlement=derive_fill_settlement(
            evidence,
            observation_identity=observation_identity,
        ),
        order_lock_key=_stable_signed_int64(order_scope),
        partition_key=partition_key,
    )


def derive_fill_settlement(
    evidence: NormalizedBrokerFillEvidence,
    *,
    observation_identity: str,
) -> NormalizedFillSettlement:
    """Fingerprint the revisable post-trade values of one observed fill.

    ``evidence_source``, ``evidence_ref``, and ``observed_at`` are provenance of
    the poll rather than settlement values, so they stay out of the hash. That
    keeps a repeated poll of unchanged settlement idempotent instead of
    appending a revision per poll.
    """
    return NormalizedFillSettlement(
        settlement_hash=_sha256(
            {
                "schema": _SETTLEMENT_SCHEMA,
                "observation_identity": observation_identity,
                "cumulative_quantity": _decimal_text(evidence.cumulative_quantity),
                "fill_quantity": _decimal_text(evidence.fill_quantity),
                "average_price": _decimal_text(evidence.average_price),
                "last_fill_price": _decimal_text(evidence.last_fill_price),
                "cumulative_notional": _decimal_text(evidence.cumulative_notional),
                "fee_total": _decimal_text(evidence.fee_total),
                "filled_at": _datetime_text(evidence.filled_at),
            }
        ),
        cumulative_quantity=evidence.cumulative_quantity,
        reported_fill_quantity=evidence.fill_quantity,
        average_price=evidence.average_price,
        last_fill_price=evidence.last_fill_price,
        cumulative_notional=evidence.cumulative_notional,
        fee_total=evidence.fee_total,
        filled_at=evidence.filled_at,
    )


def derive_projection_delivery_key(
    *,
    projection_name: str,
    observation_identity: str,
) -> str:
    return _sha256(
        {
            "schema": _DELIVERY_SCHEMA,
            "projection_name": projection_name,
            "observation_identity": observation_identity,
        }
    )


def derive_projection_lock_key(*, projection_name: str, partition_key: str) -> int:
    return _stable_signed_int64(
        {
            "schema": _PROJECTION_LOCK_SCHEMA,
            "projection_name": projection_name,
            "partition_key": partition_key,
        }
    )


__all__ = [
    "derive_fill_observation_identity",
    "derive_fill_settlement",
    "derive_projection_delivery_key",
    "derive_projection_lock_key",
    "has_positive_fill",
    "normalize_fill_evidence",
]
