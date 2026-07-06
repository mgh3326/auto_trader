"""Durable broker execution ledger models (ROB-211)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.trading import InstrumentType

NOW_SQL = text("now()")
FILLED_AT_DESC = text("filled_at DESC")
STARTED_AT_DESC = text("started_at DESC")


class ExecutionLedger(Base):
    __tablename__ = "execution_ledger"
    __table_args__ = (
        UniqueConstraint(
            "broker",
            "account_mode",
            "venue",
            "broker_order_id",
            "fill_seq",
            name="uq_execution_ledger_fill",
        ),
        CheckConstraint("broker IN ('kis','upbit')", name="execution_ledger_broker"),
        CheckConstraint(
            "account_mode IN ('live','mock')", name="execution_ledger_account_mode"
        ),
        CheckConstraint("side IN ('buy','sell')", name="execution_ledger_side"),
        CheckConstraint("currency IN ('KRW','USD')", name="execution_ledger_currency"),
        CheckConstraint(
            "source IN ('reconciler','websocket','manual_import')",
            name="execution_ledger_source",
        ),
        CheckConstraint("fill_seq >= 0", name="execution_ledger_fill_seq_nonnegative"),
        CheckConstraint("filled_qty > 0", name="execution_ledger_filled_qty_positive"),
        CheckConstraint(
            "filled_price > 0", name="execution_ledger_filled_price_positive"
        ),
        Index("ix_execution_ledger_filled_at", FILLED_AT_DESC),
        Index("ix_execution_ledger_symbol_filled_at", "symbol", FILLED_AT_DESC),
        Index("ix_execution_ledger_broker_filled_at", "broker", FILLED_AT_DESC),
        Index("ix_execution_ledger_source_id", "source", "id"),
        Index("ix_execution_ledger_source_run_id", "source_run_id"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    broker: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False, default="live")
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, name="instrument_type", create_type=False), nullable=False
    )
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    raw_symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    fill_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filled_qty: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    filled_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    filled_notional: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    fee_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    fee_currency: Mapped[str | None] = mapped_column(Text)
    filled_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="reconciler")
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    raw_payload_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=NOW_SQL,
        onupdate=NOW_SQL,
        nullable=False,
    )


class ExecutionLedgerReconcileRun(Base):
    __tablename__ = "execution_ledger_reconcile_runs"
    __table_args__ = (
        CheckConstraint(
            "broker IN ('kis','upbit')", name="execution_ledger_runs_broker"
        ),
        Index("ix_execution_ledger_runs_broker_window", "broker", "window_start"),
        Index("ix_execution_ledger_runs_started_at", STARTED_AT_DESC),
        {"schema": "review"},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    broker: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    would_insert: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    would_update: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_insert: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_update: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
