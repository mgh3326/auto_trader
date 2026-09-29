"""Operator-declared long-term quantity floors for live broker accounts (#728).

The head is intentionally mutable only through ``ProtectedQuantityService``;
the revision table is the durable append-only evidence trail.  These models do
not represent broker fills or order-ledger state, and must never be used to
rewrite either of them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class ProtectedPosition(Base):
    """Current protection declaration for one live account/market/symbol key."""

    __tablename__ = "protected_positions"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "market",
            "symbol",
            name="uq_protected_position_scope_market_symbol",
        ),
        CheckConstraint(
            "account_scope IN ('kis_live','toss_live','upbit_live')",
            name="scope",
        ),
        CheckConstraint("market IN ('kr','us','crypto')", name="market"),
        CheckConstraint(
            "(account_scope = 'upbit_live' AND market = 'crypto') "
            "OR (account_scope IN ('kis_live','toss_live') AND market IN ('kr','us'))",
            name="scope_market",
        ),
        CheckConstraint("length(btrim(symbol)) > 0", name="symbol_nonempty"),
        CheckConstraint("protected_quantity >= 0", name="quantity_nonnegative"),
        CheckConstraint(
            "purpose IN ('long_term')",
            name="purpose",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(
            "last_confirmed_broker_held >= 0",
            name="confirmed_held_nonnegative",
        ),
        Index(
            "ix_protected_position_scope_market_symbol",
            "account_scope",
            "market",
            "symbol",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_scope: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    protected_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    purpose: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="long_term"
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_confirmed_broker_held: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ProtectedPositionRevision(Base):
    """Immutable operator evidence for a protection head revision."""

    __tablename__ = "protected_position_revisions"
    __table_args__ = (
        UniqueConstraint(
            "protected_position_id",
            "revision",
            name="uq_protected_position_revision",
        ),
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_protected_position_revision_actor_idempotency",
        ),
        CheckConstraint(
            "action IN ('declare','increase','decrease','release','reconfirm')",
            name="action",
        ),
        CheckConstraint("new_quantity >= 0", name="new_nonnegative"),
        CheckConstraint(
            "previous_quantity IS NULL OR previous_quantity >= 0",
            name="previous_nonnegative",
        ),
        CheckConstraint("broker_held_observed >= 0", name="held_nonnegative"),
        CheckConstraint(
            "broker_sellable_observed >= 0",
            name="sellable_nonnegative",
        ),
        CheckConstraint(
            "length(btrim(reason)) > 0",
            name="reason_nonempty",
        ),
        CheckConstraint("origin IN ('invest_ui','operator_cli')", name="origin"),
        CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="idempotency_nonempty",
        ),
        Index(
            "ix_protected_position_revision_position_recorded",
            "protected_position_id",
            "recorded_at",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protected_position_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "review.protected_positions.id",
            name="fk_pp_revisions_position",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    previous_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    new_quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    broker_held_observed: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False
    )
    broker_sellable_observed: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False
    )
    broker_observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["ProtectedPosition", "ProtectedPositionRevision"]
