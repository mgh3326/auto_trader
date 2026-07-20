"""ROB-971 scheduleless, env-gated investment-watch expiry sweep."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.core.timezone import now_kst
from app.mcp_server.tooling.investment_reports_handlers import run_expired_watches_sweep

logger = logging.getLogger(__name__)


# Mirrors ROB-897: registered manually, without schedule=, until operators
# explicitly approve a production recurrence after manual reps.
@broker.task(task_name="review.investment_watch_expire_sweep")
async def sweep_expired_watches_task() -> dict[str, Any]:
    if not settings.watch_expire_sweep_enabled:
        return {"status": "disabled", "expired": 0}
    try:
        result = await run_expired_watches_sweep(now=now_kst())
    except Exception as exc:  # pragma: no cover - defensive task boundary
        logger.error(
            "TaskIQ investment-watch expiry sweep failed: %s", exc, exc_info=True
        )
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "expired": result["expired_count"]}
