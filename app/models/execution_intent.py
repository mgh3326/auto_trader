"""ROB-1440 — stored execution intents and the child request write-ahead journal.

Intents are immutable once inserted. A child row commits before any broker call;
``sending`` therefore means "a forward may have happened, outcome not recorded".
Fills are never recorded here. They stay in the broker ledgers and execution
ledger, which only broker evidence can write.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExecutionIntent(Base):
    __tablename__ = "execution_intents"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="schema_version"),
        CheckConstraint("account IN ('kis_mock')", name="account_mock_only"),
        CheckConstraint("venue IN ('krx')", name="venue"),
        CheckConstraint("side IN ('buy', 'sell')", name="side"),
        CheckConstraint("max_quantity BETWEEN 1 AND 1000000", name="max_quantity"),
        CheckConstraint("max_notional_krw BETWEEN 1 AND 5000000", name="max_notional"),
        CheckConstraint("limit_price_krw BETWEEN 1 AND 10000000", name="limit_price"),
        CheckConstraint("max_slippage_bps BETWEEN 0 AND 500", name="slippage"),
        CheckConstraint(
            "char_length(strategy) BETWEEN 1 AND 128 "
            "AND char_length(policy_version) BETWEEN 1 AND 128 "
            "AND char_length(authorization_ref) BETWEEN 1 AND 128",
            name="references",
        ),
        CheckConstraint(
            "position_at_authorization >= 0 AND min_position_after >= 0 "
            "AND min_position_after <= max_position_after",
            name="inventory",
        ),
        {"schema": "review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    max_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_notional_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_price_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_slippage_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    position_at_authorization: Mapped[int] = mapped_column(BigInteger, nullable=False)
    min_position_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_position_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str | None] = mapped_column(Text)
    authorization_ref: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    intent_digest: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionChildRequest(Base):
    __tablename__ = "execution_child_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_execution_child_requests_key"),
        CheckConstraint("action IN ('submit', 'amend', 'cancel')", name="action"),
        CheckConstraint(
            "state IN ('sending', 'accepted', 'uncertain', 'refused')", name="state"
        ),
        CheckConstraint(
            "(state = 'refused') = (refusal_reason IS NOT NULL)", name="refusal"
        ),
        CheckConstraint(
            "(state = 'sending') = (finished_at IS NULL)", name="finished_at"
        ),
        CheckConstraint(
            "(action = 'submit') = (target_idempotency_key IS NULL)", name="target"
        ),
        CheckConstraint(
            "(action = 'cancel' AND quantity IS NULL AND limit_price_krw IS NULL) "
            "OR (action <> 'cancel' AND quantity > 0 AND limit_price_krw > 0)",
            name="order_values",
        ),
        Index("ix_execution_child_requests_intent", "intent_id", "id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_intents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_idempotency_key: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int | None] = mapped_column(BigInteger)
    limit_price_krw: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
