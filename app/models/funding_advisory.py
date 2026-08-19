"""Persistence models for the funding-advisory domain.

External cash is an operator-declared, append-only snapshot.  It is advisory
display evidence only; order buying power, sizing, caps, and approval decisions
must continue to use broker-authoritative inputs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ExternalCashDeclaration(Base):
    """One immutable operator declaration of cash outside a broker account."""

    __tablename__ = "external_cash_declarations"
    __table_args__ = (
        UniqueConstraint("declaration_id", name="uq_external_cash_declaration_id"),
        UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_external_cash_owner_idempotency",
        ),
        UniqueConstraint(
            "supersedes_declaration_id",
            name="uq_external_cash_supersedes",
        ),
        CheckConstraint("amount >= 0", name="ck_external_cash_amount_nonnegative"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_external_cash_currency"),
        CheckConstraint(
            "length(btrim(location_key)) > 0",
            name="ck_external_cash_location_nonempty",
        ),
        CheckConstraint(
            "length(btrim(display_label)) > 0",
            name="ck_external_cash_label_nonempty",
        ),
        CheckConstraint(
            "length(btrim(source_note)) > 0",
            name="ck_external_cash_note_nonempty",
        ),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="ck_external_cash_idempotency_nonempty",
        ),
        CheckConstraint(
            "fresh_until > as_of", name="ck_external_cash_fresh_after_asof"
        ),
        CheckConstraint("origin = 'invest_ui'", name="ck_external_cash_origin"),
        Index(
            "ix_external_cash_owner_location_currency_asof",
            "owner_user_id",
            "location_key",
            "currency",
            "as_of",
            "recorded_at",
        ),
        Index(
            "ix_external_cash_supersedes_declaration_id",
            "supersedes_declaration_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    declaration_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    location_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_label: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    as_of: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    fresh_until: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    declared_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="invest_ui"
    )
    supersedes_declaration_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "review.external_cash_declarations.declaration_id",
            ondelete="RESTRICT",
        ),
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FundingAdvisory(Base):
    """Mutable lifecycle head for one candidate/gate/account funding thread."""

    __tablename__ = "funding_advisories"
    __table_args__ = (
        UniqueConstraint("advisory_id", name="uq_funding_advisory_id"),
        UniqueConstraint("thread_key", name="uq_funding_advisory_thread_key"),
        CheckConstraint(
            "market IN ('crypto','equity_kr','equity_us')",
            name="ck_funding_advisory_market",
        ),
        CheckConstraint("side = 'buy'", name="ck_funding_advisory_buy_only"),
        CheckConstraint(
            "state IN ('active','resolved','superseded')",
            name="ck_funding_advisory_state",
        ),
        Index("ix_funding_advisory_owner_state", "owner_user_id", "state"),
        Index(
            "ix_funding_advisory_candidate",
            "source_kind",
            "source_candidate_id",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    advisory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    thread_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_candidate_id: Mapped[str] = mapped_column(Text, nullable=False)
    gate_name: Mapped[str] = mapped_column(Text, nullable=False)
    gate_version: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    target_account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    broker_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False, server_default="buy")
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    valid_until: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FundingAdvisoryRevision(Base):
    """Immutable calculation snapshot for one advisory thread."""

    __tablename__ = "funding_advisory_revisions"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_funding_advisory_revision_id"),
        UniqueConstraint(
            "advisory_id",
            "revision_no",
            name="uq_funding_advisory_revision_no",
        ),
        UniqueConstraint(
            "advisory_id",
            "fingerprint",
            name="uq_funding_advisory_fingerprint",
        ),
        CheckConstraint(
            "required_cash >= 0 AND target_buying_power >= 0 "
            "AND other_pending_required >= 0 AND reserved_cash >= 0 "
            "AND shortfall >= 0 AND operational_gap >= 0",
            name="ck_funding_revision_amounts_nonnegative",
        ),
        Index(
            "ix_funding_revision_advisory_evaluated",
            "advisory_id",
            "evaluated_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    advisory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.funding_advisories.advisory_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    required_cash: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    target_buying_power: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False
    )
    other_pending_required: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, server_default="0"
    )
    reserved_cash: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, server_default="0"
    )
    shortfall: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    operational_gap: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    routes: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    combination: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FundingAdvisoryDelivery(Base):
    """Per-channel/day claim that enforces the notification cap."""

    __tablename__ = "funding_advisory_deliveries"
    __table_args__ = (
        UniqueConstraint("delivery_id", name="uq_funding_delivery_id"),
        UniqueConstraint(
            "advisory_id",
            "channel",
            "kst_date",
            name="uq_funding_delivery_advisory_channel_date",
        ),
        CheckConstraint("channel = 'telegram'", name="ck_funding_delivery_channel"),
        CheckConstraint(
            "state IN ('claimed','sent','send_failed','edit_failed','delivery_unknown')",
            name="ck_funding_delivery_state",
        ),
        Index("ix_funding_delivery_state", "state", "kst_date"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    advisory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.funding_advisories.advisory_id", ondelete="RESTRICT"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="telegram"
    )
    kst_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="claimed")
    revision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "review.funding_advisory_revisions.revision_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    chat_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    failure_code: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class FundingAdvisoryProposalLink(Base):
    """Provenance-only link; never an order classifier or sizing input."""

    __tablename__ = "funding_advisory_proposal_links"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_funding_link_proposal_id"),
        Index("ix_funding_link_advisory_id", "advisory_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    advisory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.funding_advisories.advisory_id", ondelete="RESTRICT"),
        nullable=False,
    )
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("review.order_proposals.proposal_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="order_proposal_create"
    )
    linked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
