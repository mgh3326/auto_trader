"""ROB-207 TaskIQ task registration tests."""
from __future__ import annotations

import pytest


@pytest.mark.unit
def test_task_module_is_discovered_by_taskiq_init():
    import app.tasks as tasks_pkg
    from app.tasks import research_reports_ingest_tasks

    assert research_reports_ingest_tasks in tasks_pkg.TASKIQ_TASK_MODULES


@pytest.mark.unit
def test_task_has_no_active_recurring_schedule():
    """Scheduler activation is approval-gated; the registered task ships scheduleless."""
    from app.tasks.research_reports_ingest_tasks import research_reports_ingest_bulk_smoke

    labels = getattr(research_reports_ingest_bulk_smoke, "labels", {}) or {}
    schedule = labels.get("schedule") if isinstance(labels, dict) else None
    assert not schedule, f"Schedule must be empty until approval. Found: {schedule!r}"


@pytest.mark.unit
def test_task_default_invocation_is_dry_run(monkeypatch):
    import asyncio
    from app.tasks import research_reports_ingest_tasks as mod

    captured = {}

    async def fake_runner(*, payload_file: str, commit: bool):
        captured["payload_file"] = payload_file
        captured["commit"] = commit
        return {"status": "completed", "committed": commit}

    monkeypatch.setattr(mod, "run_research_reports_ingest", fake_runner)
    result = asyncio.get_event_loop().run_until_complete(
        mod.research_reports_ingest_bulk_smoke(payload_file="/some/path.json")
    )
    assert captured["commit"] is False
    assert result["committed"] is False
