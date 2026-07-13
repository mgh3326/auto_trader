"""ROB-298 — Internal repository for BinanceDemoOrderLedger.

Service-internal. Never import this from outside
``app/services/brokers/binance/demo/ledger/``. The AST guard in
``tests/services/brokers/binance/demo/test_ledger_service.py``
will fail if you do.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import (
    BLOCKING_ROOT_LIFECYCLE_STATES,
    BinanceDemoOrderLedger,
)
from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo.errors import (
    BinanceDemoDuplicateAcknowledgement,
)

# Lifecycle states that block starting a new lifecycle for a symbol: a
# row is either in flight (planned..filled) or in an unresolved anomaly.
# closed / reconciled / cancelled free the slot (cooldown then spaces
# re-entry). Single source of truth for read-side "is this open?" — shared
# with the model's partial-unique index predicate (ROB-844).
OPEN_LIFECYCLE_STATES: tuple[str, ...] = BLOCKING_ROOT_LIFECYCLE_STATES

# ROB-844 — stable, process-wide 64-bit key for the transaction-scoped advisory
# lock that serializes root-entry reservations across the single configured Demo
# account scope (TaskIQ / MCP / websocket are distinct processes on one DB, so an
# in-memory guard cannot serialize them — a Postgres advisory lock can). Derived
# deterministically from a namespace string so every process computes the same
# key without a migration or shared constant table.
_ROOT_RESERVATION_LOCK_KEY: int = int.from_bytes(
    hashlib.sha256(b"binance_demo_order_ledger:root_reservation").digest()[:8],
    "big",
    signed=True,
)

# Normalized, stable reservation outcomes (never leak IntegrityError upward).
RESERVATION_RESERVED = "reserved"
RESERVATION_EXPOSURE_SLOT_TAKEN = "exposure_slot_taken"
RESERVATION_REPLAYED = "replayed"
RESERVATION_IDEMPOTENCY_IN_PROGRESS = "idempotency_in_progress"
RESERVATION_IDEMPOTENCY_COLLISION = "idempotency_collision"

_IDEMPOTENCY_TERMINAL_STATES = frozenset(
    {
        "cancelled",
        "reconciled",
        "anomaly",
    }
)
_PAPER_EXECUTION_IDENTITY_KEY = "paper_execution_identity"


@dataclass(frozen=True)
class RootReservationResult:
    """Outcome of an atomic root-entry reservation (ROB-844).

    ``status`` is one of ``reserved`` / ``replayed`` /
    ``idempotency_in_progress`` / ``idempotency_collision`` /
    ``exposure_slot_taken``. ``row`` is the inserted or existing root when the
    outcome has native evidence. ``reason`` narrows a blocked outcome.
    """

    status: str
    row: BinanceDemoOrderLedger | None = None
    reason: str | None = None


class BinanceDemoLedgerRepository:
    """Direct DB surface for the demo order ledger. Service-internal."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        now: dt.datetime,
    ) -> BinanceDemoOrderLedger:
        """Insert a new ledger row in the ``planned`` lifecycle state."""
        row = BinanceDemoOrderLedger(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            parent_client_order_id=parent_client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            lifecycle_state="planned",
            planned_at=now,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def reserve_root_planned(
        self,
        *,
        instrument_id: int,
        product: str,
        venue_host: str,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        idempotency_metadata: dict[str, Any] | None = None,
        global_open_root_cap: int,
        now: dt.datetime,
    ) -> RootReservationResult:
        """Atomically claim a root exposure slot and insert its ``planned`` row.

        ROB-844 — closes the count→insert TOCTOU. Under a transaction-scoped
        advisory lock (serializing every reserver on the single Demo account
        scope) this re-checks, *in the same transaction*:

        1. a deterministic client-order-id replay/collision, when immutable
           idempotency metadata is supplied,
        2. the global open-*root* cap (``parent_client_order_id IS NULL`` only),
        3. an existing open *root* for this ``(product, instrument)``,

        then inserts the planned root row. Transaction ownership belongs to the
        service's dedicated reservation session: this repository neither commits
        nor rolls back its caller. A loser returns a normalized
        ``exposure_slot_taken`` without ever inserting. A constraint conflict
        (defense-in-depth partial unique index) is caught in a savepoint and
        normalized identically — no ``IntegrityError`` escapes.

        The row is always a **root** (``parent_client_order_id`` is never set
        here); close/reduce-only child legs use ``insert_planned`` and never
        consume a slot.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _ROOT_RESERVATION_LOCK_KEY},
        )

        stored_metadata = dict(extra_metadata or {})
        if idempotency_metadata is not None:
            immutable_metadata = dict(idempotency_metadata)
            existing = await self.get_by_client_order_id(client_order_id)
            if existing is not None:
                existing_identity = (existing.extra_metadata or {}).get(
                    _PAPER_EXECUTION_IDENTITY_KEY
                )
                if existing_identity != immutable_metadata:
                    return RootReservationResult(
                        status=RESERVATION_IDEMPOTENCY_COLLISION,
                        row=existing,
                        reason="immutable_metadata_mismatch",
                    )
                if existing.lifecycle_state in _IDEMPOTENCY_TERMINAL_STATES:
                    return RootReservationResult(
                        status=RESERVATION_REPLAYED,
                        row=existing,
                        reason="terminal_replay",
                    )
                return RootReservationResult(
                    status=RESERVATION_IDEMPOTENCY_IN_PROGRESS,
                    row=existing,
                    reason="root_inflight",
                )
            # The separately supplied immutable metadata is authoritative.  It
            # cannot be omitted or replaced inside the caller's general ledger
            # evidence dictionary.
            stored_metadata[_PAPER_EXECUTION_IDENTITY_KEY] = immutable_metadata

        global_open = await self.count_open_root_lifecycles()
        if global_open >= global_open_root_cap:
            return RootReservationResult(
                status=RESERVATION_EXPOSURE_SLOT_TAKEN,
                reason="global_open_root_cap",
            )

        if await self.has_open_root_lifecycle_for_instrument(
            product=product, instrument_id=instrument_id
        ):
            return RootReservationResult(
                status=RESERVATION_EXPOSURE_SLOT_TAKEN,
                reason="instrument_open_root",
            )

        row = BinanceDemoOrderLedger(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            parent_client_order_id=None,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            lifecycle_state="planned",
            planned_at=now,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=stored_metadata or None,
        )
        try:
            # The partial-unique fallback must not poison the dedicated outer
            # transaction. The advisory lock is transaction-scoped and remains
            # held until the service exits that transaction.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            # Only the named defense-in-depth open-root race is a normal slot
            # loss. FK/check/numeric/client-id failures are product or schema
            # faults and must remain visible to operators, never masquerade as
            # ``exposure_slot_taken``.
            if "uq_binance_demo_ledger_open_root" in str(exc.orig):
                return RootReservationResult(
                    status=RESERVATION_EXPOSURE_SLOT_TAKEN,
                    reason="unique_conflict",
                )
            raise

        return RootReservationResult(status=RESERVATION_RESERVED, row=row)

    async def count_open_root_lifecycles(self) -> int:
        """Count blocking *root* lifecycles table-wide (``parent`` IS NULL only).

        Root-only so close/reduce-only child legs never consume the global
        entry cap (ROB-844 AC#6).
        """
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(
                BinanceDemoOrderLedger.parent_client_order_id.is_(None),
                BinanceDemoOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES),
            )
        )
        return count or 0

    async def has_open_root_lifecycle_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> bool:
        """True if a blocking *root* lifecycle exists for ``(product, instrument)``.

        Root-only so a close/reduce-only child never blocks a genuine re-entry
        and, conversely, never occupies the per-instrument root slot (ROB-844).
        """
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(
                BinanceDemoOrderLedger.product == product,
                BinanceDemoOrderLedger.instrument_id == instrument_id,
                BinanceDemoOrderLedger.parent_client_order_id.is_(None),
                BinanceDemoOrderLedger.lifecycle_state.in_(OPEN_LIFECYCLE_STATES),
            )
        )
        return (count or 0) > 0

    async def get_by_client_order_id(
        self, client_order_id: str, *, for_update: bool = False
    ) -> BinanceDemoOrderLedger | None:
        """Return the row matching ``client_order_id`` or ``None``."""
        stmt = select(BinanceDemoOrderLedger).where(
            BinanceDemoOrderLedger.client_order_id == client_order_id
        )
        if for_update:
            # ``FOR UPDATE`` acquires the database lock but SQLAlchemy otherwise
            # reuses an already-loaded identity-map instance without refreshing
            # its attributes. A long-lived executor session could therefore
            # validate a stale ``planned`` state after a reconciler committed
            # ``reconciled`` and overwrite the terminal truth. Populate the
            # locked row from the database unconditionally before transition
            # validation (ROB-844).
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Read-only queries (ROB-307 ledger-backed durable scalping state §4).
    # ------------------------------------------------------------------

    async def resolve_instrument_id(
        self, *, venue: str, product: str, venue_symbol: str
    ) -> int | None:
        """Map a ``(venue, product, venue_symbol)`` triple to instrument id."""
        return await self._session.scalar(
            select(CryptoInstrument.id).where(
                CryptoInstrument.venue == venue,
                CryptoInstrument.product == product,
                CryptoInstrument.venue_symbol == venue_symbol,
            )
        )

    async def resolve_or_create_instrument(
        self,
        *,
        venue: str,
        product: str,
        venue_symbol: str,
        base_asset: str,
        quote_asset: str,
    ) -> int:
        """Resolve an instrument identity, inserting it idempotently if absent."""
        inserted_id = await self._session.scalar(
            pg_insert(CryptoInstrument)
            .values(
                venue=venue,
                product=product,
                venue_symbol=venue_symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                status="active",
            )
            .on_conflict_do_nothing(index_elements=["venue", "product", "venue_symbol"])
            .returning(CryptoInstrument.id)
        )
        if inserted_id is not None:
            return inserted_id
        existing_id = await self.resolve_instrument_id(
            venue=venue, product=product, venue_symbol=venue_symbol
        )
        if existing_id is None:  # pragma: no cover - DB contract corruption
            raise RuntimeError("instrument upsert completed without a resolvable row")
        return existing_id

    async def count_open_lifecycles(self) -> int:
        """Count table-wide blocking *root* lifecycles (read-side telemetry).

        Root-only (``parent_client_order_id IS NULL``) so a close/reduce-only
        child leg never inflates the advisory global count — the authoritative
        cap is enforced in ``reserve_root_planned`` (ROB-844).
        """
        return await self.count_open_root_lifecycles()

    async def has_open_lifecycle_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> bool:
        """True if a blocking *root* lifecycle exists (read-side telemetry).

        Root-only twin of the authoritative reservation check; a close/reduce-
        only child never counts as an open lifecycle (ROB-844).
        """
        return await self.has_open_root_lifecycle_for_instrument(
            product=product, instrument_id=instrument_id
        )

    async def count_lifecycles_since(self, *, since: dt.datetime) -> int:
        """Count lifecycles initiated (``planned_at``) at or after ``since``."""
        count = await self._session.scalar(
            select(func.count())
            .select_from(BinanceDemoOrderLedger)
            .where(BinanceDemoOrderLedger.planned_at >= since)
        )
        return count or 0

    async def latest_close_at_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> dt.datetime | None:
        return await self._session.scalar(
            select(func.max(BinanceDemoOrderLedger.closed_at)).where(
                BinanceDemoOrderLedger.product == product,
                BinanceDemoOrderLedger.instrument_id == instrument_id,
            )
        )

    async def closed_rows_since(
        self, *, since: dt.datetime
    ) -> list[BinanceDemoOrderLedger]:
        result = await self._session.execute(
            select(BinanceDemoOrderLedger).where(
                BinanceDemoOrderLedger.closed_at >= since
            )
        )
        return list(result.scalars().all())

    async def update_state(
        self,
        row: BinanceDemoOrderLedger,
        *,
        new_state: str,
        now: dt.datetime,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        """Mutate ``row`` in place to reflect a lifecycle state transition."""

        def _apply() -> None:
            row.lifecycle_state = new_state
            row.updated_at = now
            if broker_order_id is not None:
                row.broker_order_id = broker_order_id
            if anomaly_reason is not None:
                row.anomaly_reason = anomaly_reason
            # Stamp the per-state timestamp column when known. Adding a new
            # lifecycle state (e.g., PR 2 futures states) is a one-line change
            # below — and the model must grow the matching column first.
            timestamp_col_for_state = {
                "planned": "planned_at",
                "previewed": "previewed_at",
                "validated": "validated_at",
                "submitted": "submitted_at",
                "filled": "filled_at",
                "closed": "closed_at",
                "cancelled": "cancelled_at",
                "reconciled": "reconciled_at",
                "anomaly": "anomaly_at",
            }.get(new_state)
            if timestamp_col_for_state is not None:
                setattr(row, timestamp_col_for_state, now)
            # ``reconciled`` additionally stamps ``last_reconciled_at`` so
            # repeat reconciliations can refresh the freshness signal.
            if new_state == "reconciled":
                row.last_reconciled_at = now
            if extra_metadata_merge is not None:
                merged = dict(row.extra_metadata or {})
                merged.update(extra_metadata_merge)
                row.extra_metadata = merged

        if broker_order_id is not None:
            # Attaching a broker ack can collide with the
            # ``(product, venue_host, instrument_id, broker_order_id)``
            # partial-unique index
            # when the same ack is replayed onto a second row. Flush and, on a
            # conflict, roll back its SAVEPOINT and re-raise as a typed
            # duplicate-acknowledgement — no IntegrityError leaks to the executor
            # / MCP boundary (ROB-844). The caller's transaction can contain a
            # prior lifecycle and unrelated work, so a whole-session rollback is
            # forbidden here.
            # Capture identifying fields BEFORE the flush: a rollback below
            # expires ``row``, so reading its attributes afterwards would trigger
            # a lazy-load IO outside the async greenlet.
            product_label, venue_label, instrument_label = (
                row.product,
                row.venue_host,
                row.instrument_id,
            )
            try:
                async with self._session.begin_nested():
                    _apply()
                    await self._session.flush()
            except IntegrityError as exc:
                if "uq_binance_demo_ledger_broker_ack" in str(exc.orig):
                    raise BinanceDemoDuplicateAcknowledgement(
                        f"broker_order_id={broker_order_id!r} already acknowledged "
                        f"for product={product_label!r} venue_host={venue_label!r} "
                        f"instrument_id={instrument_label!r}"
                    ) from exc
                raise
        else:
            _apply()
            await self._session.flush()
        return row
