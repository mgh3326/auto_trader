"""ROB-405 Slice B — paused taskiq task for auto journal verdicts.
NO schedule: paused; operator flips JOURNAL_VERDICT_AUTO_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.journal_verdict_service import (
    sync_journal_verdicts,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="journal_verdict.sync")  # no schedule → paused
async def journal_verdict_sync() -> dict:
    if not settings.JOURNAL_VERDICT_AUTO_ENABLED:
        return {"status": "disabled", "created": 0}
    async with AsyncSessionLocal() as db:
        return await sync_journal_verdicts(db)
