"""ROB-1440 — stored execution intents and the child request write-ahead journal.

Intents are immutable once inserted. A child row commits before any broker call;
``sending`` therefore means "a forward may have happened, outcome not recorded".
Fills are never recorded here. They stay in the broker ledgers and execution
ledger, which only broker evidence can write.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: hk 5441. A v1 row is a kis_mock KRX intent with exactly the bounds it always
#: had and none of the v2 columns; a v2 row is an upbit_live intent with decimal
#: quantities, a mandate and a sleeve. Every term is definite: a NULL can never
#: slip a CHECK through as UNKNOWN.
INTENT_ACCOUNT_BY_VERSION_SQL = (
    "(schema_version = 1 AND account = 'kis_mock' AND venue = 'krx') "
    "OR (schema_version = 2 AND account = 'upbit_live' AND venue = 'upbit')"
)
INTENT_V1_SHAPE_SQL = (
    "schema_version <> 1 OR ("
    "max_quantity IS NOT NULL AND max_quantity BETWEEN 1 AND 1000000 "
    "AND max_notional_krw BETWEEN 1 AND 5000000 "
    "AND limit_price_krw BETWEEN 1 AND 10000000 "
    "AND position_at_authorization IS NOT NULL "
    "AND min_position_after IS NOT NULL AND max_position_after IS NOT NULL "
    "AND position_at_authorization >= 0 AND min_position_after >= 0 "
    "AND min_position_after <= max_position_after "
    "AND mandate_id IS NULL AND sleeve_id IS NULL AND execution_mode IS NULL "
    "AND strategy_code_sha IS NULL AND decision_ref IS NULL AND origin IS NULL "
    "AND operator_exit_id IS NULL AND max_quantity_decimal IS NULL "
    "AND position_at_authorization_decimal IS NULL "
    "AND min_position_after_decimal IS NULL "
    "AND max_position_after_decimal IS NULL)"
)
INTENT_V2_SHAPE_SQL = (
    "schema_version <> 2 OR ("
    "max_quantity IS NULL AND position_at_authorization IS NULL "
    "AND min_position_after IS NULL AND max_position_after IS NULL "
    "AND mandate_id IS NOT NULL AND sleeve_id IS NOT NULL "
    "AND execution_mode IS NOT NULL AND execution_mode IN ('shadow', 'live') "
    "AND strategy_code_sha IS NOT NULL AND strategy_code_sha ~ '^[0-9a-f]{40}$' "
    "AND decision_ref IS NOT NULL AND char_length(decision_ref) BETWEEN 1 AND 128 "
    "AND origin IS NOT NULL AND origin IN ('strategy', 'operator_exit') "
    "AND (origin = 'operator_exit') = (operator_exit_id IS NOT NULL) "
    "AND (origin <> 'operator_exit' OR side = 'sell') "
    "AND symbol ~ '^KRW-[A-Z0-9]{2,10}$' "
    "AND max_quantity_decimal IS NOT NULL AND max_quantity_decimal > 0 "
    "AND max_quantity_decimal <= 1000000 "
    "AND max_notional_krw BETWEEN 5000 AND 1000000000000 "
    "AND limit_price_krw BETWEEN 1 AND 10000000000 "
    "AND position_at_authorization_decimal IS NOT NULL "
    "AND min_position_after_decimal IS NOT NULL "
    "AND max_position_after_decimal IS NOT NULL "
    "AND position_at_authorization_decimal >= 0 "
    "AND min_position_after_decimal >= 0 "
    "AND min_position_after_decimal <= max_position_after_decimal)"
)
CHILD_ORDER_VALUES_SQL = (
    "(action = 'cancel' AND quantity IS NULL AND quantity_decimal IS NULL "
    "AND limit_price_krw IS NULL) "
    "OR (action <> 'cancel' AND limit_price_krw IS NOT NULL AND limit_price_krw > 0 "
    "AND ((quantity IS NOT NULL AND quantity > 0 AND quantity_decimal IS NULL) "
    "OR (quantity IS NULL AND quantity_decimal IS NOT NULL "
    "AND quantity_decimal > 0)))"
)


class ExecutionIntent(Base):
    __tablename__ = "execution_intents"
    __table_args__ = (
        CheckConstraint("schema_version IN (1, 2)", name="schema_version"),
        CheckConstraint(INTENT_ACCOUNT_BY_VERSION_SQL, name="account_by_version"),
        CheckConstraint("side IN ('buy', 'sell')", name="side"),
        CheckConstraint("max_slippage_bps BETWEEN 0 AND 500", name="slippage"),
        CheckConstraint(
            "char_length(strategy) BETWEEN 1 AND 128 "
            "AND char_length(policy_version) BETWEEN 1 AND 128 "
            "AND char_length(authorization_ref) BETWEEN 1 AND 128",
            name="references",
        ),
        CheckConstraint(INTENT_V1_SHAPE_SQL, name="v1_shape"),
        CheckConstraint(INTENT_V2_SHAPE_SQL, name="v2_shape"),
        Index("ix_execution_intents_sleeve", "sleeve_id"),
        {"schema": "review"},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    strategy: Mapped[str] = mapped_column(Text, nullable=False)
    account: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    # v1 only (whole shares); NULL on a v2 row.
    max_quantity: Mapped[int | None] = mapped_column(BigInteger)
    max_notional_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_price_krw: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_slippage_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    position_at_authorization: Mapped[int | None] = mapped_column(BigInteger)
    min_position_after: Mapped[int | None] = mapped_column(BigInteger)
    max_position_after: Mapped[int | None] = mapped_column(BigInteger)
    # v2 only (upbit_live, hk 5441); NULL on a v1 row.
    mandate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_mandates.id", ondelete="RESTRICT"),
    )
    sleeve_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("review.strategy_sleeves.id", ondelete="RESTRICT")
    )
    execution_mode: Mapped[str | None] = mapped_column(Text)
    strategy_code_sha: Mapped[str | None] = mapped_column(Text)
    decision_ref: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(Text)
    operator_exit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review.execution_operator_exits.id", ondelete="RESTRICT"),
    )
    max_quantity_decimal: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    position_at_authorization_decimal: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8)
    )
    min_position_after_decimal: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    max_position_after_decimal: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
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
            "state IN ('sending', 'accepted', 'uncertain', 'refused', "
            "'shadowed', 'not_sent')",
            name="state",
        ),
        CheckConstraint(
            "(state IN ('refused', 'not_sent')) = (refusal_reason IS NOT NULL)",
            name="refusal",
        ),
        CheckConstraint(
            "(state = 'sending') = (finished_at IS NULL)", name="finished_at"
        ),
        CheckConstraint(
            "(action = 'submit') = (target_idempotency_key IS NULL)", name="target"
        ),
        CheckConstraint(CHILD_ORDER_VALUES_SQL, name="order_values"),
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
    #: The Upbit quantity (hk 5441); ``quantity`` is NULL on such a row.
    quantity_decimal: Mapped[Decimal | None] = mapped_column(Numeric(28, 8))
    limit_price_krw: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text)
    broker_order_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
