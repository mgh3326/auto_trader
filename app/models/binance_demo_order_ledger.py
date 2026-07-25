"""ROB-298 — ORM model for ``binance_demo_order_ledger``.

Unified Demo-oriented order lifecycle ledger. Keyed by a ``product``
discriminator ('spot' | 'usdm_futures'). PR 1 writes only 'spot' rows;
PR 2 adds 'usdm_futures'.

All writes must go through
``app.services.brokers.binance.demo.ledger.service.BinanceDemoLedgerService``;
the repository (``BinanceDemoLedgerRepository``) is service-internal and
guarded by an AST-scanning test (see ``test_ledger_service.py``).

State vocabulary (CHECK-constrained at DB layer):

    planned → previewed → validated → submitted → filled → closed → reconciled
    (with cancelled and anomaly branches)

Service layer enforces the transition graph; DB only validates the bag
of allowed strings.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# ROB-844 — the root lifecycle states that still occupy an exposure slot: a
# row is either in flight (planned..filled) or in an unresolved anomaly.
# ``closed``/``cancelled``/``reconciled`` free the slot. Single source of
# truth shared by the repository read-side count *and* the partial-unique
# index predicate below (keep the two in lockstep — the DB index is the
# defense-in-depth twin of the transactional recount).
BLOCKING_ROOT_LIFECYCLE_STATES: tuple[str, ...] = (
    "planned",
    "previewed",
    "validated",
    "submitted",
    "filled",
    "anomaly",
)

# Rendered into the partial-index predicate as a SQL ``IN`` list. Values are
# a fixed vocabulary (never user input), so literal interpolation is safe and
# keeps the index predicate a single source with the tuple above.
_BLOCKING_ROOT_STATES_SQL = ", ".join(
    f"'{state}'" for state in BLOCKING_ROOT_LIFECYCLE_STATES
)


class BinanceDemoOrderLedger(Base):
    """Unified lifecycle ledger for Binance Spot Demo and USD-M Futures Demo."""

    __tablename__ = "binance_demo_order_ledger"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id",
            name="uq_binance_demo_ledger_client_order_id",
        ),
        CheckConstraint(
            "product IN ('spot','usdm_futures')",
            name="binance_demo_ledger_product",
        ),
        CheckConstraint(
            "lifecycle_state IN ("
            "'planned','previewed','validated','submitted','filled',"
            "'closed','cancelled','reconciled','anomaly'"
            ")",
            name="binance_demo_ledger_lifecycle_state",
        ),
        CheckConstraint("side IN ('BUY','SELL')", name="binance_demo_ledger_side"),
        CheckConstraint(
            "order_type IN ('LIMIT','MARKET')",
            name="binance_demo_ledger_order_type",
        ),
        Index("ix_binance_demo_ledger_product", "product"),
        Index("ix_binance_demo_ledger_instrument_id", "instrument_id"),
        Index("ix_binance_demo_ledger_broker_order_id", "broker_order_id"),
        Index("ix_binance_demo_ledger_lifecycle_state", "lifecycle_state"),
        Index("ix_binance_demo_ledger_created_at", "created_at"),
        Index(
            "ix_binance_demo_ledger_parent_client_order_id",
            "parent_client_order_id",
        ),
        # ROB-844 defense-in-depth #1 — at most one *blocking root* lifecycle
        # per (product, instrument). Root == ``parent_client_order_id IS NULL``;
        # close/reduce-only child legs carry a parent and are excluded, so they
        # never consume a root exposure slot. This is the DB twin of the
        # transactional recount in ``reserve_root_planned`` — it fail-closes a
        # duplicate even if two writers somehow bypass the advisory lock.
        Index(
            "uq_binance_demo_ledger_open_root",
            "product",
            "instrument_id",
            unique=True,
            postgresql_where=text(
                "parent_client_order_id IS NULL "
                f"AND lifecycle_state IN ({_BLOCKING_ROOT_STATES_SQL})"
            ),
        ),
        # ROB-844 defense-in-depth #2 — a non-null broker acknowledgement
        # ``(product, venue_host, instrument_id, broker_order_id)`` may be
        # attached to exactly one ledger row. Binance order ids can repeat in a
        # different symbol sequence, while a same-instrument replay must not
        # populate a second row.
        Index(
            "uq_binance_demo_ledger_broker_ack",
            "product",
            "venue_host",
            "instrument_id",
            "broker_order_id",
            unique=True,
            postgresql_where=text("broker_order_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    instrument_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "crypto_instruments.id",
            name="fk_binance_demo_ledger_instrument_id_crypto_instruments",
        ),
        nullable=False,
    )

    # Discriminator: 'spot' (PR 1) or 'usdm_futures' (PR 2).
    product: Mapped[str] = mapped_column(Text, nullable=False)

    # The host this row was written against — evidence trail to confirm
    # demo-api.binance.com vs demo-fapi.binance.com.
    venue_host: Mapped[str] = mapped_column(Text, nullable=False)

    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_client_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    side: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)

    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    tp_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    sl_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)

    lifecycle_state: Mapped[str] = mapped_column(Text, nullable=False)

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

    anomaly_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    anomaly_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    notional_usdt: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    notional_override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    extra_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
