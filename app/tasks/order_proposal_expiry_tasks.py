from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.core.timezone import now_kst
from app.mcp_server.tooling.order_proposal_tools import (
    run_order_proposal_expire_sweep,
    run_order_proposal_night_sweep,
)
from app.services.order_proposals.night_sweep import NIGHT_SWEEP_CRONS

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


# #1112: the 16:30 / 07:00 KST night sweep. The cron is DECLARED here the way
# other sweeps declare theirs, but the labels attach only when
# ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED is true (default false) at import
# time, and the body additionally requires ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED
# (default false). This repo registers and enables nothing; activation (flags +
# scheduler restart) is the desk's decision.
def _night_sweep_schedule_labels() -> list[dict[str, str]]:
    if not settings.order_proposal_night_sweep_schedule_enabled:
        return []
    return [{"cron": cron, "cron_offset": "Asia/Seoul"} for cron in NIGHT_SWEEP_CRONS]


@broker.task(
    task_name="order_proposal.night_sweep",
    schedule=_night_sweep_schedule_labels(),
)
async def order_proposal_night_sweep_task() -> dict[str, Any]:
    if not settings.order_proposal_night_sweep_enabled:
        return {"status": "disabled", "swept": 0, "inferred": 0}
    try:
        result = await run_order_proposal_night_sweep(now=now_kst())
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("TaskIQ order_proposal night sweep failed: %s", exc, exc_info=True)
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "ok",
        "swept": result["expiry"].get("swept_count", 0),
        "inferred": result["inference"].get("applied", 0),
        "inference_blocked": len(result["inference"].get("blocked_rows", [])),
    }
