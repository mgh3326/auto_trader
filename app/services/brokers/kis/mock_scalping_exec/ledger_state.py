"""ROB-843 — order-history state for the KIS mock scalping final risk gate.

Reads *order-history* facts from ``review.kis_mock_order_ledger`` — daily order
count, realized loss today, and time since the last close/reconcile (cooldown).
It deliberately does **not** infer held positions from order lifecycle rows: an
``accepted``/``pending``/``fill`` order row is an order, not proof of a durable
position (a filled buy that is later sold is flat). The authoritative open
position / position count comes from a fresh KIS mock holdings snapshot in the
risk gate; this module only supplies order-history counters.

Read-only: no writes, no broker/network I/O. Any DB fault propagates so the
gate fail-closes to zero broker mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.timezone import now_kst
from app.models.review import KISMockOrderLedger

_CLOSED_STATE = "reconciled"


@dataclass(frozen=True)
class MockOrderHistory:
    """Order-history counters for the risk gate (NOT position state)."""

    orders_today: int
    realized_loss_today_krw: Decimal
    seconds_since_last_close_for_symbol: float | None


def _start_of_kst_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def load_kis_mock_order_history(
    *, symbol: str, now: datetime | None = None
) -> MockOrderHistory:
    """Read daily order count, realized loss, and cooldown basis for ``symbol``."""
    from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory

    now = now or now_kst()
    since = _start_of_kst_day(now)

    async with _order_session_factory()() as db:
        orders_today = await db.scalar(
            select(func.count())
            .select_from(KISMockOrderLedger)
            .where(KISMockOrderLedger.trade_date >= since)
        )
        realized_loss = await db.scalar(
            select(func.coalesce(func.sum(-KISMockOrderLedger.net_pnl), 0)).where(
                KISMockOrderLedger.lifecycle_state == _CLOSED_STATE,
                KISMockOrderLedger.trade_date >= since,
                KISMockOrderLedger.net_pnl < 0,
            )
        )
        # Cooldown is measured from the actual close/reconcile time, so a held
        # position (no reconciled close yet) imposes no cooldown.
        last_close = await db.scalar(
            select(func.max(KISMockOrderLedger.trade_date)).where(
                KISMockOrderLedger.symbol == symbol,
                KISMockOrderLedger.lifecycle_state == _CLOSED_STATE,
            )
        )

    seconds_since_close: float | None = None
    if last_close is not None:
        seconds_since_close = (now - last_close).total_seconds()

    return MockOrderHistory(
        orders_today=int(orders_today or 0),
        realized_loss_today_krw=Decimal(str(realized_loss or 0)),
        seconds_since_last_close_for_symbol=seconds_since_close,
    )
