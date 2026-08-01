"""Immutable broker fill observations and durable projection state (ROB-1195)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import conv
from sqlalchemy.sql import func

from app.models.base import Base
from app.models.trading import InstrumentType

FILL_OBSERVATION_IDENTITY_KINDS: tuple[str, ...] = (
    "broker_fill_sequence",
    "cumulative_quantity",
)
FILL_PROJECTION_OUTBOX_STATES: tuple[str, ...] = (
    "pending",
    "processing",
    "retry",
    "succeeded",
)

_SHA256_PATTERN = "^[0-9a-f]{64}$"
_IDENTITY_KINDS_SQL = ",".join(
    f"'{value}'" for value in FILL_OBSERVATION_IDENTITY_KINDS
)
_OUTBOX_STATES_SQL = ",".join(f"'{value}'" for value in FILL_PROJECTION_OUTBOX_STATES)


class FillObservation(Base):
    """One positive fill delta proven by broker evidence.

    Rows are append-only at the database boundary. Re-observing an existing
    identity is handled by the service and never creates a second row.

    The economic columns hold the settlement values as they were first observed
    and never change. Later provider revisions are appended to
    ``review.fill_settlement_enrichments`` instead of mutating this row.
    """

    __tablename__ = "fill_observations"
    __table_args__ = (
        UniqueConstraint(
            "observation_identity",
            name="uq_fill_observation_identity",
        ),
        CheckConstraint(
            f"observation_identity ~ '{_SHA256_PATTERN}' "
            f"AND fill_fact_hash ~ '{_SHA256_PATTERN}'",
            name=conv("ck_fill_observation_hashes"),
        ),
        CheckConstraint(
            f"identity_kind IN ({_IDENTITY_KINDS_SQL})",
            name=conv("ck_fill_observation_identity_kind"),
        ),
        CheckConstraint(
            "(identity_kind = 'broker_fill_sequence' "
            "AND broker_fill_sequence IS NOT NULL "
            "AND btrim(broker_fill_sequence) <> '') "
            "OR (identity_kind = 'cumulative_quantity' "
            "AND broker_fill_sequence IS NULL "
            "AND cumulative_quantity IS NOT NULL)",
            name=conv("ck_fill_observation_identity_source"),
        ),
        CheckConstraint(
            "fill_delta_quantity > 0 "
            "AND (cumulative_quantity IS NULL OR cumulative_quantity > 0) "
            "AND (reported_fill_quantity IS NULL "
            "OR reported_fill_quantity >= 0)",
            name=conv("ck_fill_observation_positive_quantity"),
        ),
        CheckConstraint(
            "(average_price IS NULL OR average_price > 0) "
            "AND (last_fill_price IS NULL OR last_fill_price > 0) "
            "AND (cumulative_notional IS NULL OR cumulative_notional >= 0) "
            "AND (fee_total IS NULL OR fee_total >= 0)",
            name=conv("ck_fill_observation_nonnegative_economics"),
        ),
        CheckConstraint(
            "side IN ('buy','sell')",
            name=conv("ck_fill_observation_side"),
        ),
        CheckConstraint(
            "btrim(broker) <> '' AND btrim(account_ref) <> '' "
            "AND btrim(account_mode) <> '' AND btrim(venue) <> '' "
            "AND btrim(order_id) <> '' AND btrim(symbol) <> '' "
            "AND btrim(currency) <> '' AND btrim(evidence_source) <> '' "
            "AND btrim(evidence_ref) <> ''",
            name=conv("ck_fill_observation_nonblank_scope"),
        ),
        Index(
            "ix_fill_observation_order_stream",
            "broker",
            "account_ref",
            "order_id",
            "id",
        ),
        Index(
            "ix_fill_observation_created_at",
            text("created_at DESC"),
            text("id DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    observation_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    broker: Mapped[str] = mapped_column(String(32), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    account_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    venue: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_fill_sequence: Mapped[str | None] = mapped_column(String(128))
    cumulative_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    reported_fill_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    fill_delta_quantity: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False
    )
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    last_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cumulative_notional: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    fee_total: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    evidence_source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    fill_fact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    filled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    correlation_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FillSettlementEnrichment(Base):
    """One durable revision of the post-trade values for a recorded fill.

    Rows are append-only, so the full provider revision history is retained.
    ``revision`` is dense and 1-based per observation; the highest revision is
    the latest settlement the provider reported. A repeated poll whose values
    equal the latest revision appends nothing.
    """

    __tablename__ = "fill_settlement_enrichments"
    __table_args__ = (
        UniqueConstraint(
            "fill_observation_id",
            "revision",
            name="uq_fill_settlement_enrichment_revision",
        ),
        CheckConstraint(
            f"settlement_hash ~ '{_SHA256_PATTERN}'",
            name=conv("ck_fill_settlement_enrichment_hash"),
        ),
        CheckConstraint(
            "revision >= 1",
            name=conv("ck_fill_settlement_enrichment_revision"),
        ),
        CheckConstraint(
            "(cumulative_quantity IS NULL OR cumulative_quantity > 0) "
            "AND (reported_fill_quantity IS NULL "
            "OR reported_fill_quantity >= 0) "
            "AND (average_price IS NULL OR average_price > 0) "
            "AND (last_fill_price IS NULL OR last_fill_price > 0) "
            "AND (cumulative_notional IS NULL OR cumulative_notional >= 0) "
            "AND (fee_total IS NULL OR fee_total >= 0)",
            name=conv("ck_fill_settlement_enrichment_economics"),
        ),
        CheckConstraint(
            "btrim(evidence_source) <> '' AND btrim(evidence_ref) <> ''",
            name=conv("ck_fill_settlement_enrichment_nonblank_evidence"),
        ),
        Index(
            "ix_fill_settlement_enrichment_latest",
            "fill_observation_id",
            text("revision DESC"),
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fill_observation_id: Mapped[int] = mapped_column(
        ForeignKey(
            "review.fill_observations.id",
            ondelete="RESTRICT",
            name="fk_fill_settlement_enrichment_observation",
        ),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settlement_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cumulative_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    reported_fill_quantity: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    last_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cumulative_notional: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    fee_total: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    filled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    evidence_source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FillProjectionOutbox(Base):
    """Durable per-projection delivery state for one fill observation."""

    __tablename__ = "fill_projection_outbox"
    __table_args__ = (
        UniqueConstraint(
            "delivery_key",
            name="uq_fill_projection_outbox_delivery_key",
        ),
        UniqueConstraint(
            "projection_name",
            "fill_observation_id",
            name="uq_fill_projection_outbox_observation",
        ),
        CheckConstraint(
            f"delivery_key ~ '{_SHA256_PATTERN}' "
            f"AND partition_key ~ '{_SHA256_PATTERN}'",
            name=conv("ck_fill_projection_outbox_hashes"),
        ),
        CheckConstraint(
            f"state IN ({_OUTBOX_STATES_SQL})",
            name=conv("ck_fill_projection_outbox_state"),
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name=conv("ck_fill_projection_outbox_attempt_count"),
        ),
        CheckConstraint(
            "(state = 'processing' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'processing' AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name=conv("ck_fill_projection_outbox_lease"),
        ),
        CheckConstraint(
            "(state = 'succeeded' AND completed_at IS NOT NULL) "
            "OR (state <> 'succeeded' AND completed_at IS NULL)",
            name=conv("ck_fill_projection_outbox_completion"),
        ),
        Index(
            "ix_fill_projection_outbox_ready",
            "projection_name",
            "state",
            "available_at",
            "fill_observation_id",
        ),
        Index(
            "ix_fill_projection_outbox_partition",
            "projection_name",
            "partition_key",
            "fill_observation_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_key: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_name: Mapped[str] = mapped_column(String(128), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(64), nullable=False)
    fill_observation_id: Mapped[int] = mapped_column(
        ForeignKey(
            "review.fill_observations.id",
            ondelete="RESTRICT",
            name="fk_fill_projection_outbox_observation",
        ),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class FillProjectionCursor(Base):
    """Last atomically completed observation for a projection partition."""

    __tablename__ = "fill_projection_cursors"
    __table_args__ = (
        CheckConstraint(
            f"partition_key ~ '{_SHA256_PATTERN}' "
            f"AND last_observation_identity ~ '{_SHA256_PATTERN}'",
            name=conv("ck_fill_projection_cursor_hashes"),
        ),
        CheckConstraint(
            "btrim(projection_name) <> ''",
            name=conv("ck_fill_projection_cursor_projection_name"),
        ),
        Index(
            "ix_fill_projection_cursor_observation",
            "last_fill_observation_id",
        ),
        {"schema": "review"},
    )

    projection_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    partition_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_fill_observation_id: Mapped[int] = mapped_column(
        ForeignKey(
            "review.fill_observations.id",
            ondelete="RESTRICT",
            name="fk_fill_projection_cursor_observation",
        ),
        nullable=False,
    )
    last_observation_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    advanced_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = [
    "FILL_OBSERVATION_IDENTITY_KINDS",
    "FILL_PROJECTION_OUTBOX_STATES",
    "FillObservation",
    "FillProjectionCursor",
    "FillProjectionOutbox",
    "FillSettlementEnrichment",
]
