from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings
from app.jobs.paper_cohort import run_active_paper_cohorts
from app.tasks import paper_cohort_tasks

pytestmark = pytest.mark.unit


def test_paper_cohort_flag_is_default_off() -> None:
    assert Settings.model_fields["PAPER_COHORT_ENABLED"].default is False
    assert settings.PAPER_COHORT_ENABLED is False


def test_schedule_labels_are_empty_while_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    assert paper_cohort_tasks._scheduled_paper_cohort_labels() == []


def test_schedule_labels_use_configured_utc_cron_only_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_COHORT_CRON", "*/5 * * * *")
    assert paper_cohort_tasks._scheduled_paper_cohort_labels() == [
        {"cron": "*/5 * * * *", "cron_offset": "UTC"}
    ]


@pytest.mark.asyncio
async def test_disabled_task_does_not_construct_or_call_job(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    job = AsyncMock()
    monkeypatch.setattr(paper_cohort_tasks, "run_active_paper_cohorts", job)

    result = await paper_cohort_tasks.run_paper_cohorts()

    assert result == {"status": "disabled", "cohorts": []}
    job.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_job_does_not_open_database_session(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    session_factory = AsyncMock(side_effect=AssertionError("must stay unused"))

    result = await run_active_paper_cohorts(session_factory=session_factory)

    assert result == {"status": "disabled", "cohorts": []}
    session_factory.assert_not_called()
