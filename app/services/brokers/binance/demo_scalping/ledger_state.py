"""ROB-307 PR1 — ledger-backed durable state for the scalping risk gates.

Builds a :class:`LedgerSnapshot` from ``binance_demo_order_ledger`` via
the sanctioned :class:`BinanceDemoLedgerService` read surface. Cooldown
and "one open lifecycle per product+symbol" are therefore authoritative
in the DB — they survive a fresh process or scheduler run (§4). No
in-memory state, no writes here.

``realized_loss_today`` is summed from a forward-compatible convention:
closed rows whose ``extra_metadata['realized_pnl_usdt']`` is negative
contribute their magnitude. Until PR2 writes that key, the sum is 0.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import Decimal

from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import (
    LedgerSnapshot,
    Product,
)

_VENUE = "binance"


def _start_of_day_utc(now: dt.datetime) -> dt.datetime:
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _realized_loss_today(rows: Iterable[BinanceDemoOrderLedger]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        meta = row.extra_metadata or {}
        raw = meta.get("realized_pnl_usdt")
        if raw is None:
            continue
        pnl = Decimal(str(raw))
        if pnl < 0:
            total += -pnl
    return total


async def load_ledger_snapshot(
    service: BinanceDemoLedgerService,
    *,
    product: Product,
    symbol: str,
    now: dt.datetime,
) -> LedgerSnapshot:
    """Read the durable scalping risk state for ``product``/``symbol``."""

    instrument_id = await service.resolve_instrument_id(
        venue=_VENUE, product=product, venue_symbol=symbol
    )
    if instrument_id is None:
        has_open = False
        last_close: dt.datetime | None = None
    else:
        has_open = await service.has_open_lifecycle_for_instrument(
            product=product, instrument_id=instrument_id
        )
        last_close = await service.latest_close_at_for_instrument(
            product=product, instrument_id=instrument_id
        )

    since = _start_of_day_utc(now)
    global_open = await service.count_open_lifecycles()
    orders_today = await service.count_lifecycles_since(since=since)
    realized_loss = _realized_loss_today(await service.closed_rows_since(since=since))

    seconds_since_close: float | None = None
    if last_close is not None:
        seconds_since_close = (now - last_close).total_seconds()

    return LedgerSnapshot(
        has_open_lifecycle_for_symbol=has_open,
        global_open_lifecycle_count=global_open,
        orders_today=orders_today,
        realized_loss_today_usdt=realized_loss,
        seconds_since_last_close_for_symbol=seconds_since_close,
    )
