"""ROB-506 — news_relevance.judge_pending task gating/registration."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings


@pytest.mark.unit
def test_async_judgment_settings_default_off() -> None:
    fields = Settings.model_fields
    assert fields["NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED"].default is False
    assert fields["NEWS_RELEVANCE_JUDGMENT_WEBHOOK_URL"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TOKEN"].default == ""
    assert fields["NEWS_RELEVANCE_JUDGMENT_TIMEOUT_S"].default == 120.0
    assert fields["NEWS_RELEVANCE_JUDGMENT_BATCH_LIMIT"].default == 50


@pytest.mark.unit
def test_task_module_is_registered() -> None:
    from app.tasks import TASKIQ_TASK_MODULES, news_relevance_judgment_tasks

    assert news_relevance_judgment_tasks in TASKIQ_TASK_MODULES
    assert (
        news_relevance_judgment_tasks.news_relevance_judge_pending.task_name
        == "news_relevance.judge_pending"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_mode_refused_while_flag_off(monkeypatch) -> None:
    from app.tasks.news_relevance_judgment_tasks import (
        news_relevance_judge_pending,
    )

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", False)
    result = await news_relevance_judge_pending(market="kr", dry_run=False)
    assert result["status"] == "disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_allowed_while_flag_off(monkeypatch) -> None:
    import app.tasks.news_relevance_judgment_tasks as task_module

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", False)
    fake = AsyncMock(return_value={"status": "dry_run"})
    monkeypatch.setattr(task_module, "run_news_relevance_judgment", fake)
    result = await task_module.news_relevance_judge_pending(
        market="kr", symbol="035420", dry_run=True
    )
    assert result == {"status": "dry_run"}
    fake.assert_awaited_once_with(
        market="kr", symbol="035420", article_ids=None, limit=None, dry_run=True
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_commit_mode_runs_when_flag_on(monkeypatch) -> None:
    import app.tasks.news_relevance_judgment_tasks as task_module

    monkeypatch.setattr(settings, "NEWS_RELEVANCE_ASYNC_JUDGMENT_ENABLED", True)
    fake = AsyncMock(return_value={"status": "judged"})
    monkeypatch.setattr(task_module, "run_news_relevance_judgment", fake)
    result = await task_module.news_relevance_judge_pending(
        market="kr", symbol="035420", dry_run=False
    )
    assert result == {"status": "judged"}
    fake.assert_awaited_once_with(
        market="kr", symbol="035420", article_ids=None, limit=None, dry_run=False
    )
