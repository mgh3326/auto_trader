"""NHPLUG (NH namuh) Stage 2 mock order lifecycle ledger.

One row per order operation (place, modify, cancel) on the broker-verified
``acct_type=03`` mock account.  All writes go through
``app.services.nhplug_mock.ledger_service.NHPlugMockLedgerService``; no direct
SQL INSERT/UPDATE/DELETE.

Evidence-first: a row is created in ``submitting`` *before* the broker leg,
atomically claimed (``dispatching``, ``claim_token``) immediately before send —
the request body is built only from the claimed row — and moved to
``accepted`` only by a readable broker order number.  Fill
quantities and terminal states are written only by reconcile from broker
listing rows that two independent listing scopes agree on.

The DB itself pins the Stage 2 scope: ``broker='nhplug'``,
``account_mode='nhplug_mock'``, ``venue='KRX'``, and ``order_type='limit'``.
No account number is stored.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base

NHPLUG_MOCK_LEDGER_STATUSES: tuple[str, ...] = (
    "submitting",
    "dispatching",
    "not_submitted",
    "accepted",
    "acceptance_uncertain",
    "rejected",
    "open",
    "partially_filled",
    "filled",
    "cancelled",
    "modified",
    "confirmed",
    "anomaly",
)
NHPLUG_MOCK_RECONCILE_STATES: tuple[str, ...] = (
    "pending",
    "verified",
    "unknown",
    "source_disagreement",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{value}'" for value in values) + ")"


class NHPlugMockOrderLedger(Base):
    __tablename__ = "nhplug_mock_order_ledger"
    __table_args__ = (
        UniqueConstraint("client_request_id", name="uq_nhplug_mock_ledger_request"),
        UniqueConstraint(
            "order_date",
            "broker_order_id",
            name="uq_nhplug_mock_ledger_date_broker_order",
        ),
        UniqueConstraint("claim_token", name="uq_nhplug_mock_ledger_claim_token"),
        CheckConstraint("broker = 'nhplug'", name="broker_nhplug"),
        CheckConstraint("account_mode = 'nhplug_mock'", name="account_mode_mock"),
        CheckConstraint("venue = 'KRX'", name="venue_krx"),
        CheckConstraint("order_type = 'limit'", name="order_type_limit"),
        CheckConstraint(
            "operation_kind IN ('place','modify','cancel')", name="operation_kind"
        ),
        CheckConstraint("side IS NULL OR side IN ('buy','sell')", name="side"),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="symbol_krx"),
        CheckConstraint("order_date ~ '^[0-9]{8}$'", name="order_date_format"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint("price IS NULL OR price > 0", name="price_positive"),
        CheckConstraint(
            "operation_kind = 'cancel' OR (quantity IS NOT NULL AND price IS NOT NULL)",
            name="place_modify_need_limit",
        ),
        CheckConstraint(
            "operation_kind = 'place' OR original_order_id IS NOT NULL",
            name="modify_cancel_need_original",
        ),
        CheckConstraint(
            "status NOT IN ('accepted','open','partially_filled','filled',"
            "'cancelled','modified','confirmed') OR broker_order_id IS NOT NULL",
            name="accepted_needs_broker_order_id",
        ),
        CheckConstraint(_in_list("status", NHPLUG_MOCK_LEDGER_STATUSES), name="status"),
        CheckConstraint(
            _in_list("reconcile_state", NHPLUG_MOCK_RECONCILE_STATES),
            name="reconcile_state",
        ),
        CheckConstraint(
            "filled_qty IS NULL OR filled_qty >= 0", name="filled_qty_nonnegative"
        ),
        CheckConstraint(
            "filled_qty IS NULL OR quantity IS NULL OR filled_qty <= quantity",
            name="filled_within_quantity",
        ),
        CheckConstraint(
            "ack_order_id IS NULL OR ack_order_id = broker_order_id",
            name="ack_matches_broker_order",
        ),
        # Durable single-use dispatch claim (#711 round 4).
        CheckConstraint(
            "(claim_token IS NULL) = (claimed_at IS NULL)", name="claim_pair"
        ),
        CheckConstraint(
            "status <> 'submitting' OR claim_token IS NULL",
            name="submitting_unclaimed",
        ),
        CheckConstraint(
            "status IN ('submitting','not_submitted') OR claim_token IS NOT NULL",
            name="dispatched_states_claimed",
        ),
        Index("ix_nhplug_mock_ledger_order_date_status", "order_date", "status"),
        Index("ix_nhplug_mock_ledger_symbol", "symbol"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    order_date: Mapped[str] = mapped_column(Text, nullable=False)
    broker: Mapped[str] = mapped_column(Text, nullable=False, default="nhplug")
    account_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="nhplug_mock"
    )
    venue: Mapped[str] = mapped_column(Text, nullable=False, default="KRX")
    operation_kind: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False, default="limit")
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    price: Mapped[Decimal | None] = mapped_column(Numeric(20, 3))

    broker_order_id: Mapped[str | None] = mapped_column(Text)
    # Set only by record_ack from a readable broker acknowledgement; a number
    # bound later by attribute matching never populates it.  Terminal
    # cancel/modify corroboration requires this positive ack evidence.
    ack_order_id: Mapped[str | None] = mapped_column(Text)
    # Set once by the atomic claim immediately before the broker leg; never
    # cleared.  An unclaimed ``submitting`` row provably never reached send.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    original_order_id: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(Text, nullable=False)
    reconcile_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'"), default="pending"
    )
    response_code: Mapped[str | None] = mapped_column(Text)
    response_message: Mapped[str | None] = mapped_column(Text)

    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 3))
    open_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    cancelled_qty: Mapped[Decimal | None] = mapped_column(Numeric(20, 0))
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    last_reconcile: Mapped[dict | None] = mapped_column(JSONB)
    requires_manual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    manual_review_reason: Mapped[str | None] = mapped_column(Text)

    strategy: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)

    reconciled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
