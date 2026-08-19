"""Persistence models for the funding-advisory domain.

External cash is an operator-declared, append-only snapshot.  It is advisory
display evidence only; order buying power, sizing, caps, and approval decisions
must continue to use broker-authoritative inputs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
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
