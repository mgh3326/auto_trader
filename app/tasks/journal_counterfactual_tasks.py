"""ROB-405 Slice C — paused taskiq task for counterfactual sync.
NO schedule: paused; operator flips JOURNAL_COUNTERFACTUAL_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.journal_counterfactual_service import (
    sync_journal_counterfactuals,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="journal_counterfactual.sync")  # no schedule → paused
async def journal_counterfactual_sync() -> dict:
    if not settings.JOURNAL_COUNTERFACTUAL_ENABLED:
        return {"status": "disabled", "created": 0}
    async with AsyncSessionLocal() as db:
        return await sync_journal_counterfactuals(db)
