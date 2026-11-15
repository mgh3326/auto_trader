"""hk 4734 (3797 T-P4) — Kiwoom mock fill → ``execution_ledger`` upsert builder.

Design: ``docs/design/3797-kiwoom-mock-ledger.md`` §2.3 and §3.4, invariants
I4 (a fill is booked only by a broker-evidence delta) and I9 (every Kiwoom row
is ``broker='kiwoom' AND account_mode='mock'``).

Pure module: an order-ledger delta in, an :class:`ExecutionLedgerUpsert` out.
No database, no broker, no clock (``observed_at`` is passed in). The write goes
through :class:`ExecutionLedgerRepository`, the only ``execution_ledger`` writer.

The Kiwoom mock TRs report no fee, no tax and no fill timestamp (design §3.1),
so the row says so: ``fee_basis='not_provided'`` and
``filled_at_basis='observed_at'``. Unknown cost is not recorded as zero.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.schemas.execution_ledger import ExecutionLedgerUpsert

__all__ = (
    "KIWOOM_ACCOUNT_MODE",
    "KIWOOM_BROKER",
    "KIWOOM_VENUE",
    "build_kiwoom_execution_ledger_upsert",
)

#: I9 — every Kiwoom mock fill row carries these fixed values.
KIWOOM_BROKER: Final = "kiwoom"
KIWOOM_ACCOUNT_MODE: Final = "mock"
KIWOOM_VENUE: Final = "krx"
KIWOOM_INSTRUMENT_TYPE: Final = "equity_kr"
KIWOOM_CURRENCY: Final = "KRW"

MAX_SQL_INT32: Final = 2_147_483_647


def _stable_int32_hash(seed: str) -> int:
    """Deterministic ``fill_seq`` (mirrors the Toss bridge's derivation)."""
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) & MAX_SQL_INT32


def build_kiwoom_execution_ledger_upsert(
    *,
    ledger_id: int,
    broker_order_id: str,
    symbol: str,
    side: str,
    previous_filled_qty: Decimal,
    filled_quantity: int,
    average_price: int,
    observed_at: datetime,
    correlation_id: str | None = None,
    previous_average_price: Decimal | None = None,
) -> ExecutionLedgerUpsert:
    """Book one broker-evidence delta of a Kiwoom mock order.

    ``previous_filled_qty`` is the cumulative quantity already booked for the
    order and ``previous_average_price`` the cumulative average that booking
    recorded; ``filled_quantity`` and ``average_price`` are the broker's
    *cumulative* filled quantity and cumulative average. The difference of the
    quantities is this row's quantity and the difference of the cumulative
    notionals its price:

        delta_price = (cum_qty*cum_avg - prev_qty*prev_avg) / delta_qty

    so the row is not priced at the cumulative average for only the incremental
    quantity — the sum of the booked notionals stays equal to the broker's
    cumulative notional. A re-run of the same broker evidence maps to the same
    ``fill_seq`` and books nothing new (I4).

    ``filled_quantity`` and ``average_price`` are the broker's exact integer KRW
    values — they are converted to ``Decimal`` without a float step.
    """
    if isinstance(filled_quantity, bool) or not isinstance(filled_quantity, int):
        raise TypeError("filled_quantity must be an exact int")
    if isinstance(average_price, bool) or not isinstance(average_price, int):
        raise TypeError("average_price must be an exact int")
    if filled_quantity <= 0:
        raise ValueError("filled_quantity must be positive")
    if average_price <= 0:
        raise ValueError("average_price must be positive")
    if previous_average_price is not None and not isinstance(
        previous_average_price, Decimal
    ):
        raise TypeError("previous_average_price must be a Decimal or None")
    if previous_average_price is not None and previous_average_price <= 0:
        raise ValueError("previous_average_price must be positive")

    cumulative = Decimal(filled_quantity)
    delta = cumulative - previous_filled_qty
    if delta <= 0:
        # A non-positive delta is not a fill. Refuse instead of booking zero or
        # a regressed quantity (the caller treats this as already booked).
        raise ValueError("delta must be positive to book a fill")

    order_id = str(broker_order_id).strip()
    if not order_id:
        raise ValueError("broker_order_id must not be blank")

    cumulative_average = Decimal(average_price)
    if previous_filled_qty > 0:
        if previous_average_price is None:
            # Without the earlier cumulative average the incremental price is
            # unknowable; refuse instead of booking the cumulative average for
            # only this delta (that would corrupt P&L inputs).
            raise ValueError(
                "previous_average_price is required when quantity was already booked"
            )
        price = (
            cumulative * cumulative_average
            - previous_filled_qty * previous_average_price
        ) / delta
        if price <= 0:
            raise ValueError("derived incremental price is not positive")
    else:
        price = cumulative_average
    fill_seq = _stable_int32_hash(
        f"{order_id}:{previous_filled_qty.normalize()}:{cumulative.normalize()}"
    )
    return ExecutionLedgerUpsert(
        broker=KIWOOM_BROKER,
        account_mode=KIWOOM_ACCOUNT_MODE,
        venue=KIWOOM_VENUE,
        instrument_type=KIWOOM_INSTRUMENT_TYPE,
        symbol=symbol,
        raw_symbol=symbol,
        side=side,
        broker_order_id=order_id,
        fill_seq=fill_seq,
        filled_qty=delta,
        filled_price=price,
        filled_notional=delta * price,
        fee_amount=None,
        fee_currency=KIWOOM_CURRENCY,
        fee_basis="not_provided",
        tax_amount=None,
        filled_at_basis="observed_at",
        filled_at=observed_at,
        currency=KIWOOM_CURRENCY,
        correlation_id=correlation_id,
        source="reconciler",
        raw_payload_json={
            "source": "kiwoom_mock_reconcile",
            "ledger_id": ledger_id,
            "previous_filled_qty": str(previous_filled_qty),
            "previous_avg_fill_price": (
                None if previous_average_price is None else str(previous_average_price)
            ),
            "broker_cumulative_filled_qty": str(cumulative),
            "broker_cumulative_avg_price": str(cumulative_average),
            "delta_fill_price": str(price),
        },
    )
