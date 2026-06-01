"""ROB-405 Slice A — paused taskiq task for the mock roundtrip journal bridge.
NO schedule: starts paused; operator flips MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED
and adds a cron in a follow-up.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.taskiq_broker import broker
from app.services.trade_journal.mock_roundtrip_journal_bridge import (
    sync_mock_roundtrip_journals,
)

logger = logging.getLogger(__name__)


@broker.task(task_name="mock_roundtrip.journal_sync")  # no schedule → paused
async def mock_roundtrip_journal_sync() -> dict:
    if not settings.MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED:
        return {"status": "disabled", "created": 0, "closed": 0}
    async with AsyncSessionLocal() as db:
        return await sync_mock_roundtrip_journals(db)
