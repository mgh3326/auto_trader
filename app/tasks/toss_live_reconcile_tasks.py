"""ROB-574 — paused TaskIQ auto-reconcile for Toss live KR/US orders.

Registered with the worker so operators can kick or externally schedule it, but
it carries no in-code ``schedule=`` label. Recurrence is owned by
robin-prefect-automations plus env gate flips after safety review.

Reuses the proven toss_reconcile_orders_impl kernel. Send-time Toss order rows
remain accepted-only; fills, journals, and realized PnL are booked only from
confirmed single-order broker evidence.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

logger = logging.getLogger(__name__)


@broker.task(task_name="toss_live.reconcile_periodic")  # no schedule -> paused
async def toss_live_reconcile_periodic() -> dict:
    if not settings.TOSS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    if not settings.TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED:
        return {
            "status": "paused",
            "message": "TOSS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False",
        }
    return await toss_reconcile_orders_impl(dry_run=False)
