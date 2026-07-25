"""Default-off TaskIQ declaration for ROB-849 cohort execution."""

from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.paper_cohort import run_active_paper_cohorts
from app.services.paper_cohort.contracts import PaperCohortError


def _scheduled_paper_cohort_labels() -> list[dict[str, str]]:
    if not settings.PAPER_COHORT_ENABLED:
        return []
    return [{"cron": settings.PAPER_COHORT_CRON, "cron_offset": "UTC"}]


@broker.task(
    task_name="paper_cohort.run_active",
    schedule=_scheduled_paper_cohort_labels(),
    retry_on_error=True,
    max_retries=3,
    delay=5,
)
async def run_paper_cohorts() -> dict[str, object]:
    result = await run_active_paper_cohorts()
    cohorts = result.get("cohorts")
    if isinstance(cohorts, list) and any(
        isinstance(item, dict) and item.get("status") == "failed" for item in cohorts
    ):
        raise PaperCohortError("paper_cohort_retryable_failure")
    return result


__all__ = ["run_paper_cohorts"]
