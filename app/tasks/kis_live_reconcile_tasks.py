"""ROB-475 / ROB-574 — paused TaskIQ auto-reconcile for KIS live KR orders.

Registered with the worker so operators can kick or externally schedule it, but
it carries no in-code ``schedule=`` label. Recurrence is owned by
robin-prefect-automations plus env gate flips after safety review.

Reuses the proven kis_live_reconcile_orders_impl kernel. The accepted-only send
gate stays intact: no fills, journals, or realized PnL are booked unless broker
evidence is confirmed by the reconcile kernel.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.mcp_server.tooling.kis_live_ledger import kis_live_reconcile_orders_impl

logger = logging.getLogger(__name__)


@broker.task(task_name="kis_live.reconcile_periodic")  # no schedule → paused
async def kis_live_reconcile_periodic() -> dict:
    if not settings.KIS_LIVE_AUTO_RECONCILE_ENABLED:
        return {
            "status": "paused",
            "message": "KIS_LIVE_AUTO_RECONCILE_ENABLED is False",
        }
    if not settings.KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED:
        return {
            "status": "paused",
            "message": "KIS_LIVE_AUTO_RECONCILE_SAFETY_REVIEW_PASSED is False",
        }
    return await kis_live_reconcile_orders_impl(dry_run=False)
