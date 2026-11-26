"""Manual, read-only broker history fetch and optional ledger booking."""

from __future__ import annotations

from typing import Any

from . import constants as C
from .ledger_service import FuturesMockOrderLedgerService
from .place import default_deps


async def reconcile_futures_mock_day(
    *, trading_day: str, commit: bool
) -> dict[str, Any]:
    import os

    from app.core.db import AsyncSessionLocal

    C.assert_limits_locked()
    if commit and os.environ.get(C.ENABLED_ENV) != "true":
        return {"status": "refused", "reason": "env_gate_disabled"}
    deps = default_deps()
    client = deps.new_order_client()
    try:
        history = await client.daily_history(
            account=deps.account_no(), trading_day=trading_day
        )
    finally:
        await client.close()
    async with AsyncSessionLocal() as db:
        orders = await FuturesMockOrderLedgerService(db).reconcile_day(
            trading_day=trading_day, history=history, commit=commit
        )
    return {
        "status": "accepted" if commit else "preview",
        "trading_day": trading_day,
        "orders": orders,
    }
