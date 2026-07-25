from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.core.timezone import now_kst
from app.mcp_server.tooling.order_proposal_tools import run_order_proposal_expire_sweep

logger = logging.getLogger(__name__)


# ROB-897: shipped scheduleless. Production recurrence is a separate decision made
# AFTER manual reps (operator/Prefect-registered, e.g. robin-prefect-automations) so
# enabling this does not silently auto-start a sweep that edits Telegram messages.
# Default off via ORDER_PROPOSAL_EXPIRE_SWEEP_ENABLED. Suggested cadence when
# registered: every few minutes during market hours -- valid_until deadlines are
# frequently intraday, not just end-of-day.
@broker.task(task_name="order_proposal.expire_sweep")
async def order_proposal_expire_sweep_task() -> dict[str, Any]:
    if not settings.order_proposal_expire_sweep_enabled:
        return {"status": "disabled", "swept": 0, "skipped": 0}
    try:
        result = await run_order_proposal_expire_sweep(now=now_kst())
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error(
            "TaskIQ order_proposal expire sweep failed: %s", exc, exc_info=True
        )
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "ok",
        "swept": result.get("swept_count", 0),
        "skipped": result.get("skipped_count", 0),
    }
