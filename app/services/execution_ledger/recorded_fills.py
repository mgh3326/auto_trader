"""Recorded fill evidence for the reconcile evidence boundary (hk 4606).

The reconcile kernels (KIS US and Toss, both ``full`` and ``terminal_only``)
must not close an order on broker terminal no-fill evidence when positive
fills were already recorded for the same order identity — either on the live
order ledger row itself or in ``execution_ledger``. This module provides the
shared read that sums the in-effect recorded quantity for one order so the
boundary classifier can mark such evidence ``contradictory``.

Order identity follows ``app/services/execution_ledger/supersede.py``:
``(broker, account_mode, instrument_type, symbol, side, currency,
normalize_order_id(broker_order_id), trading_day)``. When the reconcile row
cannot prove a field (``symbol``/``side``/``currency`` empty, or no order
time so a KIS trading day cannot be derived), that position matches any
candidate — the read errs toward counting fills, i.e. toward contradiction,
never toward silently closing the order.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.services.execution_ledger.supersede import (
    _symbol_key,
    normalize_order_id,
    order_identity,
    order_trading_day,
)


def order_identity_from_fields(
    *,
    broker: str,
    instrument_type: str,
    symbol: str | None,
    side: str | None,
    currency: str | None,
    broker_order_id: str | None,
    order_time: datetime | None,
) -> tuple[Any, ...] | None:
    """The order-side identity used to match recorded fill rows.

    Positions the reconcile row cannot prove are ``None`` and act as
    wildcards in ``order_identity_matches`` (they narrow the candidate set
    less, never more). Returns ``None`` when the order id is blank — a row
    with no broker order id can never own recorded fills.
    """
    order_id = normalize_order_id(broker_order_id)
    if not order_id:
        return None
    symbol_key = _symbol_key(instrument_type, symbol)
    side_key = str(side or "").strip().lower()
    currency_key = str(currency or "").strip().upper()
    trading_day: Any = None
    if broker == "kis" and order_time is not None:
        trading_day = order_trading_day(broker, instrument_type, order_time)
    return (
        broker,
        "live",
        instrument_type,
        symbol_key or None,
        side_key or None,
        currency_key or None,
        order_id,
        trading_day,
    )


def order_identity_matches(
    order: tuple[Any, ...], fill_identity: tuple[Any, ...]
) -> bool:
    """Fill identity belongs to the order, with ``None`` as an order-side wildcard."""
    for wanted, actual in zip(order, fill_identity, strict=True):
        if wanted is not None and wanted != actual:
            return False
    return True


async def recorded_fill_qty_for_order(
    db: AsyncSession,
    *,
    broker: str,
    instrument_type: str,
    symbol: str | None,
    side: str | None,
    currency: str | None,
    broker_order_id: str | None,
    order_time: datetime | None,
) -> Decimal:
    """Positive in-effect fill qty already recorded for this order identity."""
    order = order_identity_from_fields(
        broker=broker,
        instrument_type=instrument_type,
        symbol=symbol,
        side=side,
        currency=currency,
        broker_order_id=broker_order_id,
        order_time=order_time,
    )
    if order is None:
        return Decimal("0")
    order_id = order[6]
    result = await db.execute(
        select(ExecutionLedger).where(
            ExecutionLedger.broker == broker,
            ExecutionLedger.account_mode == "live",
            func.ltrim(ExecutionLedger.broker_order_id, "0") == order_id.lstrip("0"),
            execution_ledger_in_effect(),
        )
    )
    total = Decimal("0")
    for row in result.scalars().all():
        if order_identity_matches(order, order_identity(row)):
            total += row.filled_qty
    return total
