"""ROB-286 — BinanceTestnetLedgerService (the public write surface).

All writes to ``binance_testnet_order_ledger`` go through this service.
Direct SQL or repository imports from outside this module are forbidden;
the boundary is enforced by the test
``test_repository_import_boundary_enforced`` (AST scan over ``app/**.py``).

State lifecycle (locked transition table; raises
``BinanceInvalidStateTransition`` on any illegal move):

    planned          → previewed | anomaly
    previewed        → validated | anomaly
    validated        → submitted | anomaly
    submitted        → filled | cancelled | anomaly
    filled           → tp_sl_armed | closed | anomaly
    tp_sl_armed      → tp_sl_triggered | cancelled | anomaly
    tp_sl_triggered  → closed | anomaly
    cancelled        → reconciled | anomaly
    closed           → reconciled | anomaly
    anomaly          → reconciled   (only by operator-initiated clear)

Open item #4 lean adopted: Sentry events fire on ``anomaly`` always +
on the first ``filled`` after ``submitted`` (sanity). No noise for
routine ``previewed`` / ``validated`` / ``tp_sl_armed`` transitions.

Open item #6 lean adopted: TP and SL are represented as two separate
ledger rows linked by ``parent_client_order_id`` (both reference the
entry's client_order_id). Whichever triggers first transitions the
other to ``cancelled`` via an explicit cancel record.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.binance_testnet_order_ledger import BinanceTestnetOrderLedger
from app.services.brokers.binance.testnet.errors import (
    BinanceInvalidStateTransition,
)
from app.services.brokers.binance.testnet.ledger.repository import (
    BinanceTestnetLedgerRepository,
)

logger = logging.getLogger("app.services.brokers.binance.testnet.ledger")

# Locked transition table — single source of truth for legal moves.
LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"previewed", "anomaly"}),
    "previewed": frozenset({"validated", "anomaly"}),
    "validated": frozenset({"submitted", "anomaly"}),
    "submitted": frozenset({"filled", "cancelled", "anomaly"}),
    "filled": frozenset({"tp_sl_armed", "closed", "anomaly"}),
    "tp_sl_armed": frozenset({"tp_sl_triggered", "cancelled", "anomaly"}),
    "tp_sl_triggered": frozenset({"closed", "anomaly"}),
    "cancelled": frozenset({"reconciled", "anomaly"}),
    "closed": frozenset({"reconciled", "anomaly"}),
    "anomaly": frozenset({"reconciled"}),
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _emit_sentry_anomaly(*, client_order_id: str, reason: str) -> None:
    """Fail-open Sentry emission for anomaly transitions."""
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"binance_testnet_order_ledger anomaly "
            f"client_order_id={client_order_id} reason={reason!r}",
            level="warning",
        )
    except Exception:  # noqa: BLE001 — intentional fail-open
        return


def _emit_sentry_first_fill(*, client_order_id: str, broker_order_id: str) -> None:
    """Fail-open Sentry sanity-check emission on first fill."""
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"binance_testnet_order_ledger first_fill "
            f"client_order_id={client_order_id} "
            f"broker_order_id={broker_order_id}",
            level="info",
        )
    except Exception:  # noqa: BLE001
        return


def _assert_transition_allowed(*, from_state: str, to_state: str) -> None:
    """Raise BinanceInvalidStateTransition if ``from_state → to_state`` is illegal."""
    allowed = LIFECYCLE_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise BinanceInvalidStateTransition(
            f"Illegal lifecycle transition {from_state!r} → {to_state!r}. "
            f"Allowed from {from_state!r}: {sorted(allowed)}"
        )


class BinanceTestnetLedgerService:
    """Service-only write surface for ``binance_testnet_order_ledger``."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session
        self._repo = BinanceTestnetLedgerRepository(session=session)

    async def get_by_client_order_id(
        self, client_order_id: str
    ) -> BinanceTestnetOrderLedger | None:
        return await self._repo.get_by_client_order_id(client_order_id)

    async def list_by_instrument(
        self,
        *,
        instrument_id: int,
        lifecycle_states: list[str] | None = None,
        limit: int = 100,
    ) -> list[BinanceTestnetOrderLedger]:
        return await self._repo.list_by_instrument(
            instrument_id=instrument_id,
            lifecycle_states=lifecycle_states,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # 11 record_* methods (one per lifecycle state, plus reconcile)
    # ------------------------------------------------------------------
    async def record_plan(
        self,
        *,
        instrument_id: int,
        client_order_id: str,
        side: str,
        order_type: str,
        qty: Decimal,
        price: Decimal | None = None,
        tp_price: Decimal | None = None,
        sl_price: Decimal | None = None,
        parent_client_order_id: str | None = None,
        notional_usdt: Decimal | None = None,
        notional_override_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        """Insert a new ledger row in ``planned`` state.

        Idempotent on ``client_order_id``: if a row already exists, the
        existing row is returned (no transition, no new row).
        """
        existing = await self._repo.get_by_client_order_id(client_order_id)
        if existing is not None:
            return existing
        now = _utcnow()
        row = await self._repo.insert(
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            side=side,
            order_type=order_type,
            qty=qty,
            price=price,
            tp_price=tp_price,
            sl_price=sl_price,
            parent_client_order_id=parent_client_order_id,
            lifecycle_state="planned",
            notional_usdt=notional_usdt,
            notional_override_reason=notional_override_reason,
            extra_metadata=extra_metadata,
            now=now,
        )
        logger.info(
            "binance_testnet_order_ledger state=planned client_order_id=%s "
            "instrument_id=%s side=%s qty=%s",
            client_order_id,
            instrument_id,
            side,
            qty,
        )
        return row

    async def _transition(
        self,
        *,
        client_order_id: str,
        to_state: str,
        broker_order_id: str | None = None,
        anomaly_reason: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        row = await self._repo.get_by_client_order_id(client_order_id)
        if row is None:
            raise BinanceInvalidStateTransition(
                f"No ledger row for client_order_id={client_order_id!r}; "
                f"refusing to transition to {to_state!r}. Call record_plan first."
            )
        # Idempotent re-record: if already in the target state, no-op.
        if row.lifecycle_state == to_state:
            return row
        _assert_transition_allowed(from_state=row.lifecycle_state, to_state=to_state)
        was_submitted = row.lifecycle_state == "submitted"
        now = _utcnow()
        updated = await self._repo.update_state(
            row=row,
            new_state=to_state,
            broker_order_id=broker_order_id,
            anomaly_reason=anomaly_reason,
            extra_metadata=extra_metadata,
            now=now,
        )
        logger.info(
            "binance_testnet_order_ledger state=%s client_order_id=%s broker_order_id=%s",
            to_state,
            client_order_id,
            broker_order_id,
        )
        # Per open item #4 lean: Sentry on anomaly always; on first
        # filled after submitted as a sanity check.
        if to_state == "anomaly":
            _emit_sentry_anomaly(
                client_order_id=client_order_id, reason=anomaly_reason or ""
            )
        elif to_state == "filled" and was_submitted:
            _emit_sentry_first_fill(
                client_order_id=client_order_id,
                broker_order_id=str(broker_order_id or updated.broker_order_id or ""),
            )
        return updated

    async def record_preview(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="previewed",
            extra_metadata=extra_metadata,
        )

    async def record_validation(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="validated",
            extra_metadata=extra_metadata,
        )

    async def record_submit(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="submitted",
            broker_order_id=broker_order_id,
            extra_metadata=extra_metadata,
        )

    async def record_fill(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="filled",
            extra_metadata=extra_metadata,
        )

    async def record_tp_sl_armed(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="tp_sl_armed",
            extra_metadata=extra_metadata,
        )

    async def record_tp_sl_triggered(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="tp_sl_triggered",
            extra_metadata=extra_metadata,
        )

    async def record_closed(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="closed",
            extra_metadata=extra_metadata,
        )

    async def record_cancel(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="cancelled",
            extra_metadata=extra_metadata,
        )

    async def record_anomaly(
        self,
        *,
        client_order_id: str,
        reason: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        return await self._transition(
            client_order_id=client_order_id,
            to_state="anomaly",
            anomaly_reason=reason,
            extra_metadata=extra_metadata,
        )

    async def record_reconciled(
        self,
        *,
        client_order_id: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> BinanceTestnetOrderLedger:
        """Mark a terminal row as reconciled (post-anomaly clear or
        post-closed sweep). Also stamps ``last_reconciled_at``.
        """
        row = await self._transition(
            client_order_id=client_order_id,
            to_state="reconciled",
            extra_metadata=extra_metadata,
        )
        await self._repo.stamp_reconciled(row=row, now=_utcnow())
        return row

    async def stamp_reconciliation_run(
        self,
        *,
        client_order_id: str,
    ) -> BinanceTestnetOrderLedger | None:
        """Update ``last_reconciled_at`` for a row without transitioning state.

        Used by the reconciliation pass on runner startup to record that a
        broker-side check was performed even when no state change resulted.
        """
        row = await self._repo.get_by_client_order_id(client_order_id)
        if row is None:
            return None
        await self._repo.stamp_reconciled(row=row, now=_utcnow())
        return row
