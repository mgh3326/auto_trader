"""ROB-475 — paused taskiq periodic auto-reconcile for KIS live KR orders.

NO schedule: starts paused. An operator adds the cron in
robin-prefect-automations + flips KIS_LIVE_AUTO_RECONCILE_ENABLED in a
follow-up. Reuses the proven kis_live_reconcile_orders_impl kernel (accepted-
only send gate stays intact — ROB-395). NOT added to TASKIQ_TASK_MODULES.
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
    return await kis_live_reconcile_orders_impl(dry_run=False)
