"""ROB-1050 — paused TaskIQ auto-reconcile for US equity and Crypto live orders.

Registered with the worker so operators can kick or externally schedule it, but
it carries no in-code ``schedule=`` label. Recurrence is owned by external operator
automations plus env gate flips after safety review.

Uses market-separated wrappers to prevent cross-market scanning (ROB-1018).
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.live_order_ledger import live_reconcile_orders_impl

logger = logging.getLogger(__name__)


@broker.task(task_name="live.reconcile_us_periodic")  # no schedule -> paused
async def live_reconcile_us_periodic() -> dict[str, Any]:
    if not settings.LIVE_AUTO_RECONCILE_ENABLED:
        return {"skipped": "disabled"}

    dry_run = settings.LIVE_AUTO_RECONCILE_DRY_RUN
    try:
        return await live_reconcile_orders_impl(
            market="us",
            broker="kis",
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("live.reconcile_us_periodic task failed: %s", exc)
        return {"status": "error", "error": str(exc) or exc.__class__.__name__}


@broker.task(task_name="live.reconcile_crypto_periodic")  # no schedule -> paused
async def live_reconcile_crypto_periodic() -> dict[str, Any]:
    if not settings.LIVE_AUTO_RECONCILE_ENABLED:
        return {"skipped": "disabled"}

    dry_run = settings.LIVE_AUTO_RECONCILE_DRY_RUN
    try:
        return await live_reconcile_orders_impl(
            market="crypto",
            broker="upbit",
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("live.reconcile_crypto_periodic task failed: %s", exc)
        return {"status": "error", "error": str(exc) or exc.__class__.__name__}
