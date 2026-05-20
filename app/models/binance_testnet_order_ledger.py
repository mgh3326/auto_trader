"""ROB-286 — ORM model for ``binance_testnet_order_ledger``.

Dedicated lifecycle ledger for the testnet execution adapter. All writes
must go through ``BinanceTestnetLedgerService``; the repository
(``BinanceTestnetLedgerRepository``) is service-internal (see ROB-285's
``CryptoInstrumentHealthRepository`` for the same module-import-guard
pattern).

State vocabulary is locked by a CHECK constraint:

    planned → previewed → validated → submitted → filled →
    tp_sl_armed → tp_sl_triggered → closed → reconciled
    (with cancelled and anomaly branches; full table in Task 9 service)

Lifecycle semantics + transitions are enforced in service layer; the DB
only enforces the bag of allowed strings.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BinanceTestnetOrderLedger(Base):
    """Order lifecycle ledger for the Binance testnet execution adapter."""

    __tablename__ = "binance_testnet_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id",
            name="uq_binance_testnet_ledger_client_order_id",
        ),
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'tp_sl_armed','tp_sl_triggered','closed','cancelled',"
            "'reconciled','anomaly'"
            ")",
            name="binance_testnet_ledger_lifecycle_state",
        ),
        CheckConstraint(
            "side IN ('BUY','SELL')",
            name="binance_testnet_ledger_side",
        ),
        CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_testnet_ledger_order_type",
        ),
        Index("ix_binance_testnet_ledger_instrument_id", "instrument_id"),
        Index("ix_binance_testnet_ledger_broker_order_id", "broker_order_id"),
        Index("ix_binance_testnet_ledger_lifecycle_state", "lifecycle_state"),
        Index("ix_binance_testnet_ledger_created_at", "created_at"),
        Index(
            "ix_binance_testnet_ledger_parent_client_order_id",
            "parent_client_order_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "crypto_instruments.id",
            name="fk_binance_testnet_ledger_instrument_id_crypto_instruments",
        ),
        nullable=False,
    )

    # Per-order identifier we generate (UUID4 hex). UNIQUE.
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)

    # TP/SL pairing: when an entry fill arms both a TP-sell and SL-sell
    # ledger row, both reference the entry's client_order_id here.
    parent_client_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Binance-side order id, populated on first submit response.
    broker_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)

    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    tp_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    sl_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)

    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

    # Lifecycle timestamps. Each one is populated when the transition
    # into that state is first recorded.
    planned_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    previewed_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    validated_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    submitted_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    filled_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    tp_sl_armed_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    tp_sl_triggered_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    reconciled_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_reconciled_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # Anomaly bookkeeping
    anomaly_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    anomaly_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # Notional/override audit trail
    notional_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    notional_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Free-form JSON metadata (broker response excerpts, etc.).
    # SQLAlchemy reserves ``metadata`` on declarative base; we map a
    # Python ``extra_metadata`` attribute to the DB column ``metadata`` to
    # match the convention used by ``crypto_instruments``.
    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
