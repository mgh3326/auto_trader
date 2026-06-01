"""ROB-404 — paused taskiq periodic kis_mock reconcile (fallback to the
event-driven consumer). NO schedule: starts paused; an operator adds the cron
+ flips KIS_MOCK_RECONCILE_PERIODIC_ENABLED in a follow-up.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.jobs.kis_mock_reconciliation_job import run_kis_mock_reconciliation

logger = logging.getLogger(__name__)


@broker.task(task_name="kis_mock.reconcile_periodic")  # no schedule → paused
async def kis_mock_reconcile_periodic() -> dict:
    if not settings.KIS_MOCK_RECONCILE_PERIODIC_ENABLED:
        return {
            "status": "paused",
            "message": "KIS_MOCK_RECONCILE_PERIODIC_ENABLED is False",
        }
    async with AsyncSessionLocal() as db:
        return await run_kis_mock_reconciliation(db, dry_run=False)
