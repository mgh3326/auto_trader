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
    ColumnElement,
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

QUARANTINE_FIELDS_SQL = (
    "(quarantined_at IS NULL AND quarantine_reason IS NULL "
    "AND quarantined_by IS NULL) OR "
    "(quarantined_at IS NOT NULL AND quarantine_reason IS NOT NULL "
    "AND quarantined_by IS NOT NULL AND btrim(quarantine_reason) <> '' "
    "AND btrim(quarantined_by) <> '')"
)
QUARANTINE_SCOPE_SQL = (
    "quarantined_at IS NULL OR (source = 'websocket' AND broker = 'kis')"
)


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
        CheckConstraint(
            "broker IN ('kis','upbit','toss')", name="execution_ledger_broker"
        ),
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
        # #1175: quarantine is all-or-nothing, carries a non-blank reason and
        # actor, and is only ever applied to a KIS websocket row (a phantom
        # accept notice). A reconciler, manual-import, Upbit or Toss row can
        # never be quarantined, whatever writes the UPDATE.
        CheckConstraint(QUARANTINE_FIELDS_SQL, name="quarantine_fields"),
        CheckConstraint(QUARANTINE_SCOPE_SQL, name="quarantine_scope"),
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
    # #1175: a quarantined row is kept (never deleted) but is not a fill; every
    # fill/lot/evidence reader excludes it via ``execution_ledger_in_effect``.
    quarantined_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    quarantined_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=NOW_SQL,
        onupdate=NOW_SQL,
        nullable=False,
    )


def execution_ledger_in_effect() -> ColumnElement[bool]:
    """Predicate for rows that count as fills: not quarantined (#1175).

    Every reader that treats execution_ledger rows as fill evidence (lots,
    open/same-day evidence, #1112 inference, reports, triage) must AND this
    in. Identity reads used by the upsert path deliberately do not, so a
    replayed phantom frame stays ``unchanged`` instead of being re-inserted.
    """
    return ExecutionLedger.quarantined_at.is_(None)


class ExecutionLedgerQuarantineEvent(Base):
    """Append-only audit of each quarantine commit (#1175).

    One row per quarantined ledger row, grouped by ``batch_id``. The DB
    rejects UPDATE/DELETE/TRUNCATE on this table. There is deliberately no
    foreign key to ``execution_ledger`` so the audit survives any later
    ledger maintenance; ``ledger_id`` is UNIQUE, so a row is quarantined once.

    The event also keeps the row's idempotency key (broker, account_mode,
    venue, broker_order_id, fill_seq) as a tombstone: a BEFORE INSERT trigger
    on ``execution_ledger`` quarantines any KIS websocket row re-inserted with
    a tombstoned key, so deleting a quarantined row and replaying the phantom
    frame can never bring the fill back.
    """

    __tablename__ = "execution_ledger_quarantine_events"
    __table_args__ = (
        UniqueConstraint("ledger_id", name="uq_execution_ledger_quarantine_ledger"),
        CheckConstraint("action = 'quarantine'", name="action"),
        CheckConstraint("btrim(reason) <> ''", name="reason_nonblank"),
        CheckConstraint("btrim(actor) <> ''", name="actor_nonblank"),
        Index("ix_execution_ledger_quarantine_events_batch", "batch_id"),
        Index(
            "ix_execution_ledger_quarantine_events_key",
            "broker",
            "account_mode",
            "venue",
            "broker_order_id",
            "fill_seq",
        ),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ledger_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    broker: Mapped[str] = mapped_column(Text, nullable=False)
    account_mode: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    fill_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=NOW_SQL, nullable=False
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
