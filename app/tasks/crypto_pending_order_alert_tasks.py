from __future__ import annotations

import logging
from typing import Any

from app.core.taskiq_broker import broker
from app.jobs.crypto_pending_order_alert import run_crypto_pending_order_alert

logger = logging.getLogger(__name__)


@broker.task(
    task_name="alerts.crypto.pending_orders.morning",
    schedule=[{"cron": "30 8 * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_crypto_pending_order_morning_alert_task() -> dict[str, Any]:
    """Run the 08:30 KST read-only crypto pending-order reminder."""

    try:
        return await run_crypto_pending_order_alert(execute=True)
    except Exception as exc:
        logger.error(
            "TaskIQ crypto pending-order morning alert failed: %s",
            exc,
            exc_info=True,
        )
        return {"success": False, "status": "failed", "error": str(exc)}


@broker.task(
    task_name="alerts.crypto.pending_orders.us_prep",
    schedule=[{"cron": "0 22 * * *", "cron_offset": "Asia/Seoul"}],
)
async def run_crypto_pending_order_us_prep_alert_task() -> dict[str, Any]:
    """Run the 22:00 KST read-only crypto pending-order reminder."""

    try:
        return await run_crypto_pending_order_alert(execute=True)
    except Exception as exc:
        logger.error(
            "TaskIQ crypto pending-order US-prep alert failed: %s",
            exc,
            exc_info=True,
        )
        return {"success": False, "status": "failed", "error": str(exc)}


__all__ = [
    "run_crypto_pending_order_morning_alert_task",
    "run_crypto_pending_order_us_prep_alert_task",
]
