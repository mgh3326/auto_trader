"""ROB-337 Slice 2 — scheduleless, env-gated watch validity review task.

No ``schedule=`` -> never auto-runs; manual entry point only. Gated by
``settings.WATCH_VALIDITY_REVIEW_ENABLED`` (default False) and defaults to
dry_run. Recurring activation is operator-gated (separate approval).
"""

from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.services.investment_reports.watch_validity_review import (
    WatchValidityReviewService,
)


@broker.task(task_name="review.investment_watch_validity")
async def run_watch_validity_review_task(dry_run: bool = True) -> dict:
    if not settings.WATCH_VALIDITY_REVIEW_ENABLED:
        return {"status": "disabled"}
    service = WatchValidityReviewService()
    try:
        return await service.run(dry_run=dry_run)
    finally:
        await service.close()
