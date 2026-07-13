"""Default-off TaskIQ declaration for ROB-849 cohort execution."""

from __future__ import annotations

from app.core.config import settings
from app.core.taskiq_broker import broker
from app.jobs.paper_cohort import run_active_paper_cohorts


def _scheduled_paper_cohort_labels() -> list[dict[str, str]]:
    if not settings.PAPER_COHORT_ENABLED:
        return []
    return [{"cron": settings.PAPER_COHORT_CRON, "cron_offset": "UTC"}]


@broker.task(
    task_name="paper_cohort.run_active",
    schedule=_scheduled_paper_cohort_labels(),
)
async def run_paper_cohorts() -> dict[str, object]:
    if not settings.PAPER_COHORT_ENABLED:
        return {"status": "disabled", "cohorts": []}
    return await run_active_paper_cohorts()


__all__ = ["run_paper_cohorts"]
