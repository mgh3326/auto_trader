"""Tests for TaskIQ broker startup middleware."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.core.taskiq_broker as taskiq_broker


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_init_middleware_initializes_sentry_for_scheduler(monkeypatch):
    middleware = taskiq_broker.WorkerInitMiddleware()
    middleware.broker = SimpleNamespace(
        is_worker_process=False,
        is_scheduler_process=True,
    )

    mock_init_sentry = Mock()
    monkeypatch.setattr(taskiq_broker, "init_sentry", mock_init_sentry)

    await middleware.startup()

    mock_init_sentry.assert_called_once_with(service_name="auto-trader-scheduler")
