from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.services.toss_manual_activity import run_manual_activity_sweep

logger = logging.getLogger(__name__)


# ROB-866: shipped scheduleless. Production recurrence is a separate decision made
# AFTER manual reps (operator/Prefect-registered, e.g. robin-prefect-automations) so
# enabling TOSS_API_ENABLED does not silently auto-start a sweep that sends Telegram
# alerts. Default off via TOSS_MANUAL_ACTIVITY_SWEEP_ENABLED. Suggested cadence when
# registered: hourly during market hours.
@broker.task(task_name="toss.manual_activity_sweep")
async def toss_manual_activity_sweep_task(window_hours: int = 24) -> dict[str, Any]:
    if not settings.toss_manual_activity_sweep_enabled:
        return {"status": "disabled", "found": 0, "alerted": 0}
    try:
        result = await run_manual_activity_sweep(
            window_hours=window_hours, dry_run=False
        )
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("TaskIQ Toss manual-activity sweep failed: %s", exc, exc_info=True)
        return {"status": "failed", "error": str(exc)}
    found = len(result.get("manual_filled", [])) + len(result.get("manual_open", []))
    return {
        "status": "ok",
        "found": found,
        "alerted": result.get("alerted_count", 0),
    }
