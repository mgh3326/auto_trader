"""Typed service contracts for the ROB-1195 fill-observation boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.models.trading import InstrumentType


class FillObservationWriteStatus(StrEnum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    NO_DELTA = "no_delta"
    NO_FILL_EVIDENCE = "no_fill_evidence"
    WRITER_DISABLED = "writer_disabled"


class FillDualReadStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NEW_ONLY = "new_only"
    LEGACY_ONLY = "legacy_only"
    EMPTY = "empty"


class FillSettlementStatus(StrEnum):
    """Outcome of the settlement-enrichment branch of one write.

    Settlement values are post-trade corrections (fees, refined average price,
    settled fill timestamp). They never mutate the immutable observation and
    never create a second fill delta.
    """

    RECORDED = "recorded"
    UNCHANGED = "unchanged"
    ABSENT = "absent"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class BrokerFillEvidence:
    """Sanitized broker facts supplied by a future reconcile adapter.

    The contract intentionally contains no broker client or raw response. The
    stable ``evidence_ref`` points at retained broker/native-ledger evidence;
    callers must not put credentials or an unredacted broker payload here.
    """

    broker: str
    account_ref: str
    account_mode: str
    venue: str
    order_id: str
    instrument_type: InstrumentType | str
    symbol: str
    side: str
    currency: str
    evidence_source: str
    evidence_ref: str
    observed_at: datetime
    broker_fill_sequence: str | None = None
    cumulative_quantity: Decimal | int | str | None = None
    fill_quantity: Decimal | int | str | None = None
    average_price: Decimal | int | str | None = None
    last_fill_price: Decimal | int | str | None = None
    cumulative_notional: Decimal | int | str | None = None
    fee_total: Decimal | int | str | None = None
    filled_at: datetime | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedBrokerFillEvidence:
    broker: str
    account_ref: str
    account_mode: str
    venue: str
    order_id: str
    instrument_type: InstrumentType
    symbol: str
    side: str
    currency: str
    evidence_source: str
    evidence_ref: str
    observed_at: datetime
    broker_fill_sequence: str | None
    cumulative_quantity: Decimal | None
    fill_quantity: Decimal | None
    average_price: Decimal | None
    last_fill_price: Decimal | None
    cumulative_notional: Decimal | None
    fee_total: Decimal | None
    filled_at: datetime | None
    correlation_id: str | None


@dataclass(frozen=True, slots=True)
class NormalizedFillSettlement:
    """Post-trade values a provider may still revise for a settled fill.

    These are deliberately outside ``fill_fact_hash``. A broker that returns a
    late fee, a refined average price, or a corrected settlement timestamp for
    an already recorded fill is reporting the same fill, not a contradiction.
    """

    settlement_hash: str
    cumulative_quantity: Decimal | None
    reported_fill_quantity: Decimal | None
    average_price: Decimal | None
    last_fill_price: Decimal | None
    cumulative_notional: Decimal | None
    fee_total: Decimal | None
    filled_at: datetime | None

    @property
    def has_values(self) -> bool:
        """Return whether the provider supplied any settlement value."""
        return any(
            value is not None
            for value in (
                self.cumulative_quantity,
                self.reported_fill_quantity,
                self.average_price,
                self.last_fill_price,
                self.cumulative_notional,
                self.fee_total,
                self.filled_at,
            )
        )


@dataclass(frozen=True, slots=True)
class FillObservationIdentity:
    value: str
    kind: str
    fill_fact_hash: str
    settlement: NormalizedFillSettlement
    order_lock_key: int
    partition_key: str


@dataclass(frozen=True, slots=True)
class FillObservationWriteResult:
    status: FillObservationWriteStatus
    observation_identity: str | None
    observation_id: int | None
    fill_delta_quantity: Decimal
    outbox_count: int
    settlement_status: FillSettlementStatus = FillSettlementStatus.NOT_APPLICABLE
    settlement_revision: int | None = None


@dataclass(frozen=True, slots=True)
class FillProjectionDelivery:
    outbox_id: int
    delivery_key: str
    projection_name: str
    partition_key: str
    fill_observation_id: int
    observation_identity: str
    attempt_count: int
    lease_token: uuid.UUID


@dataclass(frozen=True, slots=True)
class FillDualReadValidation:
    broker: str
    account_ref: str
    account_mode: str
    venue: str
    order_id: str
    observation_count: int
    observation_quantity: Decimal
    review_trade_quantity: Decimal | None
    execution_ledger_quantity: Decimal | None
    status: FillDualReadStatus
    mismatched_sources: tuple[str, ...]


__all__ = [
    "BrokerFillEvidence",
    "FillDualReadStatus",
    "FillDualReadValidation",
    "FillObservationIdentity",
    "FillObservationWriteResult",
    "FillObservationWriteStatus",
    "FillProjectionDelivery",
    "FillSettlementStatus",
    "NormalizedBrokerFillEvidence",
    "NormalizedFillSettlement",
]
