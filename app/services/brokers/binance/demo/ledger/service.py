"""ROB-298 — Public write surface for the unified Binance Demo ledger.

All ledger writes go through this service. The repository
(``BinanceDemoLedgerRepository``) is module-internal; the AST-scanning
test ``test_repository_import_boundary_enforced`` in
``tests/services/brokers/binance/demo/test_ledger_service.py`` fails if
any ``app/**`` module outside this file imports it.

State machine (locked transition table; service raises
``BinanceDemoInvalidStateTransition`` on any illegal move)::

    planned    → previewed | cancelled | anomaly
    previewed  → validated | cancelled | anomaly
    validated  → submitted | cancelled | anomaly
    submitted  → filled    | cancelled | anomaly
    filled     → closed    | anomaly
    closed     → reconciled| anomaly
    cancelled  → reconciled| anomaly
    reconciled → (terminal)
    anomaly    → (terminal)

``product`` is restricted to ``{"spot", "usdm_futures"}`` and validated
on insert; anything else raises ``BinanceDemoInvalidProduct``. PR 1
writes only ``"spot"`` rows; PR 2 will activate ``"usdm_futures"``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.errors import (
    BinanceDemoInvalidProduct,
    BinanceDemoInvalidStateTransition,
)
from app.services.brokers.binance.demo.ledger.repository import (
    BinanceDemoLedgerRepository,
    RootReservationResult,
)

_ALLOWED_PRODUCTS = frozenset({"spot", "usdm_futures"})

# Locked transition table — single source of truth for legal moves.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"previewed", "cancelled", "anomaly"}),
    "previewed": frozenset({"validated", "cancelled", "anomaly"}),
    "validated": frozenset({"submitted", "cancelled", "anomaly"}),
    "submitted": frozenset({"filled", "cancelled", "anomaly"}),
    "filled": frozenset({"closed", "anomaly"}),
    "closed": frozenset({"reconciled", "anomaly"}),
    "cancelled": frozenset({"reconciled", "anomaly"}),
    "reconciled": frozenset(),
    "anomaly": frozenset(),
}


class BinanceDemoLedgerService:
    """Service-only write surface for ``binance_demo_order_ledger``."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = BinanceDemoLedgerRepository(session)

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceDemoOrderLedger | None:
        return await self._repo.get_by_client_order_id(client_order_id)

    # ------------------------------------------------------------------
    # Read-only surface (ROB-307 ledger-backed durable scalping state §4).
    # No writes; delegates to the service-internal repository.
    # ------------------------------------------------------------------

    async def resolve_instrument_id(
        self, *, venue: str, product: str, venue_symbol: str
    ) -> int | None:
        return await self._repo.resolve_instrument_id(
            venue=venue, product=product, venue_symbol=venue_symbol
        )

    async def count_open_lifecycles(self) -> int:
        return await self._repo.count_open_lifecycles()

    async def has_open_lifecycle_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> bool:
        return await self._repo.has_open_lifecycle_for_instrument(
            product=product, instrument_id=instrument_id
        )

    async def count_lifecycles_since(self, *, since: dt.datetime) -> int:
        return await self._repo.count_lifecycles_since(since=since)

    async def latest_close_at_for_instrument(
        self, *, product: str, instrument_id: int
    ) -> dt.datetime | None:
        return await self._repo.latest_close_at_for_instrument(
            product=product, instrument_id=instrument_id
        )

    async def closed_rows_since(
        self, *, since: dt.datetime
    ) -> list[BinanceDemoOrderLedger]:
        return await self._repo.closed_rows_since(since=since)

    async def record_planned(
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
        """Insert a new ledger row in ``planned`` state.

        Validates ``product`` against the allowed enum before touching
        the DB; an unknown ``product`` raises
        ``BinanceDemoInvalidProduct`` and never inserts a row.
        """
        if product not in _ALLOWED_PRODUCTS:
            raise BinanceDemoInvalidProduct(
                f"product={product!r} not in {sorted(_ALLOWED_PRODUCTS)}"
            )
        return await self._repo.insert_planned(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            parent_client_order_id=parent_client_order_id,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
            now=now,
        )

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
        global_open_root_cap: int,
        now: dt.datetime,
    ) -> RootReservationResult:
        """Atomically reserve a root exposure slot + insert its planned row.

        The single authoritative claim for a new root lifecycle (ROB-844): it
        re-checks the global open-root cap and the per-instrument open root
        *inside one advisory-locked transaction* and inserts the planned root,
        so concurrent TaskIQ / MCP / websocket submits cannot both pass. Returns
        a stable :class:`RootReservationResult` (``reserved`` /
        ``exposure_slot_taken``) — the caller submits to the broker only when
        the status is ``reserved``.

        Validates ``product`` first; an unknown product raises
        ``BinanceDemoInvalidProduct`` before any lock/DB work.
        """
        if product not in _ALLOWED_PRODUCTS:
            raise BinanceDemoInvalidProduct(
                f"product={product!r} not in {sorted(_ALLOWED_PRODUCTS)}"
            )
        return await self._repo.reserve_root_planned(
            instrument_id=instrument_id,
            product=product,
            venue_host=venue_host,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
            global_open_root_cap=global_open_root_cap,
            now=now,
        )

    async def _transition(
        self,
        *,
        client_order_id: str,
        new_state: str,
        now: dt.datetime,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        row = await self._repo.get_by_client_order_id(client_order_id)
        if row is None:
            raise BinanceDemoInvalidStateTransition(
                f"no ledger row for client_order_id={client_order_id!r}"
            )
        allowed = _ALLOWED_TRANSITIONS.get(row.lifecycle_state, frozenset())
        if new_state not in allowed:
            raise BinanceDemoInvalidStateTransition(
                f"{row.lifecycle_state!r} → {new_state!r} not allowed "
                f"(allowed from {row.lifecycle_state!r}: {sorted(allowed)})"
            )
        return await self._repo.update_state(
            row,
            new_state=new_state,
            now=now,
            broker_order_id=broker_order_id,
            anomaly_reason=anomaly_reason,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_previewed(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="previewed",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_validated(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="validated",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_submitted(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="submitted",
            broker_order_id=broker_order_id,
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_filled(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="filled",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_closed(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="closed",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_cancelled(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="cancelled",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_reconciled(
        self,
        *,
        client_order_id: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="reconciled",
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )

    async def record_anomaly(
        self,
        *,
        client_order_id: str,
        reason: str,
        now: dt.datetime,
        extra_metadata_merge: dict[str, Any] | None = None,
    ) -> BinanceDemoOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            new_state="anomaly",
            anomaly_reason=reason,
            now=now,
            extra_metadata_merge=extra_metadata_merge,
        )
