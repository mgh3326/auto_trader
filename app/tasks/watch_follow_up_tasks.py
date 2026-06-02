"""ROB-405 Slice E — paused taskiq task for watch follow-up linking.
NO schedule: paused; operator flips WATCH_FOLLOW_UP_LINK_ENABLED + adds cron.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.watch_follow_up_service import (
    sync_watch_follow_up_items,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="watch_follow_up.sync")  # no schedule → paused
async def watch_follow_up_sync() -> dict:
    if not settings.WATCH_FOLLOW_UP_LINK_ENABLED:
        return {"status": "disabled", "linked": 0}
    async with AsyncSessionLocal() as db:
        return await sync_watch_follow_up_items(db)
