from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings
from app.jobs.paper_cohort import run_active_paper_cohorts
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.tasks import TASKIQ_TASK_MODULES, paper_cohort_tasks

pytestmark = pytest.mark.unit


def test_paper_cohort_flag_is_default_off() -> None:
    assert Settings.model_fields["PAPER_COHORT_ENABLED"].default is False
    assert settings.PAPER_COHORT_ENABLED is False


def test_paper_cohort_task_is_discovered_from_package() -> None:
    assert paper_cohort_tasks in TASKIQ_TASK_MODULES


@pytest.mark.asyncio
async def test_disabled_direct_runner_stops_before_database_or_capture(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    session = AsyncMock()
    capture = AsyncMock()
    quotes = AsyncMock()
    runner = PaperCohortRunner(
        session,
        capture=capture,
        quote_provider=quotes,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await runner.run(
            CohortRunInvocation(
                cohort_id="cohort-disabled",
                run_id="run-disabled",
                round_decision_id="round-disabled",
                mode=RunMode.SHADOW,
            )
        )

    assert exc_info.value.reason_code == "paper_cohort_disabled"
    session.scalar.assert_not_awaited()
    session.execute.assert_not_awaited()
    capture.capture.assert_not_awaited()
    quotes.get_quote.assert_not_awaited()


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
async def test_retryable_job_failure_is_propagated_to_taskiq(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    job = AsyncMock(
        return_value={
            "status": "completed",
            "cohorts": [
                {
                    "cohort_id": "cohort-1",
                    "status": "failed",
                    "reason": "venue_quote_provider_error",
                }
            ],
        }
    )
    monkeypatch.setattr(paper_cohort_tasks, "run_active_paper_cohorts", job)

    with pytest.raises(PaperCohortError, match="paper_cohort_retryable_failure"):
        await paper_cohort_tasks.run_paper_cohorts()
    job.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_job_does_not_open_database_session(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", False)
    session_factory = AsyncMock(side_effect=AssertionError("must stay unused"))

    result = await run_active_paper_cohorts(session_factory=session_factory)

    assert result == {"status": "disabled", "cohorts": []}
    session_factory.assert_not_called()
