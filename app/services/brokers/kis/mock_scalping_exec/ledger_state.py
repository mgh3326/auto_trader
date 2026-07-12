"""ROB-843 — durable ledger state for the KIS mock scalping final risk gate.

Builds a :class:`LedgerSnapshot` from ``review.kis_mock_order_ledger`` so the
executor's pre-send ``evaluate_risk`` re-check reads authoritative DB state
(cooldown, single-position, daily order/loss caps) that survives a fresh
process or scheduler run. Read-only: no writes, no broker/network I/O.

Any DB/read fault propagates to the caller so the executor fail-closes to zero
broker mutation rather than sizing against unknown exposure.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.timezone import now_kst
from app.models.review import KISMockOrderLedger
from app.services.brokers.kis.mock_scalping.contract import LedgerSnapshot

# Lifecycle states counted as a live/open position for the risk gate.
_OPEN_STATES: frozenset[str] = frozenset({"accepted", "pending", "fill"})
_CLOSED_STATE = "reconciled"


def _start_of_kst_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def load_kis_mock_ledger_snapshot(
    *, symbol: str, now: datetime | None = None
) -> LedgerSnapshot:
    """Read the durable scalping risk state for ``symbol`` from the mock ledger."""
    from app.mcp_server.tooling.kis_mock_ledger import _order_session_factory

    now = now or now_kst()
    since = _start_of_kst_day(now)

    async with _order_session_factory()() as db:
        has_open = await db.scalar(
            select(func.count())
            .select_from(KISMockOrderLedger)
            .where(
                KISMockOrderLedger.symbol == symbol,
                KISMockOrderLedger.lifecycle_state.in_(_OPEN_STATES),
            )
        )
        open_symbols = await db.scalar(
            select(func.count(func.distinct(KISMockOrderLedger.symbol))).where(
                KISMockOrderLedger.lifecycle_state.in_(_OPEN_STATES)
            )
        )
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
        last_close = await db.scalar(
            select(func.max(KISMockOrderLedger.trade_date)).where(
                KISMockOrderLedger.symbol == symbol,
                KISMockOrderLedger.lifecycle_state == _CLOSED_STATE,
            )
        )

    seconds_since_close: float | None = None
    if last_close is not None:
        seconds_since_close = (now - last_close).total_seconds()

    return LedgerSnapshot(
        has_open_position_for_symbol=bool(has_open or 0),
        open_position_count=int(open_symbols or 0),
        orders_today=int(orders_today or 0),
        realized_loss_today_krw=Decimal(str(realized_loss or 0)),
        seconds_since_last_close_for_symbol=seconds_since_close,
    )
