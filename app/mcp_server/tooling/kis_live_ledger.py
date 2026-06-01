"""ROB-395 — KIS live order ledger writes + reconciliation.

SEND records accepted/rejected only (no trades/journal/realized_pnl). RECONCILE
applies journal mutations from order-id-keyed broker fill evidence. Fully
isolated from the mock ledger (kis_live_order_ledger vs kis_mock_order_ledger).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.mcp_server.tooling.shared import logger
from app.mcp_server.tooling.shared import to_float as _to_float
from app.models.review import KISLiveOrderLedger

# lifecycle_state mirrors status for live (no separate mock shadow semantics)
_STATUS_TO_LIFECYCLE: dict[str, str] = {
    "accepted": "accepted",
    "rejected": "failed",
    "unknown": "anomaly",
    "filled": "filled",
    "partial": "partial",
    "pending": "accepted",
    "cancelled": "cancelled",
    "anomaly": "anomaly",
}


def _status_to_lifecycle(status: str) -> str:
    return _STATUS_TO_LIFECYCLE.get(status, "anomaly")


def _derive_live_send_status(*, rt_cd: str | None, order_no: str | None) -> str:
    """Derive accepted|rejected|unknown from broker submit response.

    Never fakes success: a non-zero rt_cd is broker evidence of rejection.
    """
    if rt_cd == "0":
        return "accepted"
    if rt_cd and rt_cd != "0":
        return "rejected"
    return "accepted" if order_no else "unknown"


def _order_session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


def _to_decimal(val: Any) -> Decimal | None:
    if val in ("", None):
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None
