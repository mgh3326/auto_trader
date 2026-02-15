"""Tests for TaskIQ task status response utility."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from taskiq.exceptions import ResultGetError, ResultIsReadyError

from app.core.taskiq_result import build_task_status_response


@pytest.mark.asyncio
async def test_build_task_status_response_pending_when_not_ready():
    with (
        patch(
            "app.core.taskiq_result.broker.result_backend.is_result_ready",
            AsyncMock(return_value=False),
        ) as mock_ready,
        patch(
            "app.core.taskiq_result.broker.result_backend.get_result",
            AsyncMock(),
        ) as mock_get_result,
    ):
        response = await build_task_status_response("task-1")

    assert response == {
        "task_id": "task-1",
        "state": "PENDING",
        "ready": False,
        "is_ready": False,
    }
    mock_ready.assert_awaited_once_with("task-1")
    mock_get_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_task_status_response_success():
    task_result = SimpleNamespace(is_err=False, return_value={"ok": True})

    with (
        patch(
            "app.core.taskiq_result.broker.result_backend.is_result_ready",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.core.taskiq_result.broker.result_backend.get_result",
            AsyncMock(return_value=task_result),
        ) as mock_get_result,
    ):
        response = await build_task_status_response("task-2")

    assert response["task_id"] == "task-2"
    assert response["state"] == "SUCCESS"
    assert response["ready"] is True
    assert response["is_ready"] is True
    assert response["result"] == {"ok": True}
    mock_get_result.assert_awaited_once_with("task-2", with_logs=True)


@pytest.mark.asyncio
async def test_build_task_status_response_failure_with_task_error():
    task_result = SimpleNamespace(is_err=True, error=RuntimeError("boom"), log=None)

    with (
        patch(
            "app.core.taskiq_result.broker.result_backend.is_result_ready",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.core.taskiq_result.broker.result_backend.get_result",
            AsyncMock(return_value=task_result),
        ),
    ):
        response = await build_task_status_response("task-3")

    assert response["task_id"] == "task-3"
    assert response["state"] == "FAILURE"
    assert response["ready"] is True
    assert response["is_ready"] is True
    assert response["error"] == "boom"


@pytest.mark.asyncio
async def test_build_task_status_response_pending_on_is_ready_error():
    with (
        patch(
            "app.core.taskiq_result.broker.result_backend.is_result_ready",
            AsyncMock(side_effect=ResultIsReadyError()),
        ),
        patch(
            "app.core.taskiq_result.broker.result_backend.get_result",
            AsyncMock(),
        ) as mock_get_result,
    ):
        response = await build_task_status_response("task-4")

    assert response == {
        "task_id": "task-4",
        "state": "PENDING",
        "ready": False,
        "is_ready": False,
    }
    mock_get_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_task_status_response_failure_on_get_result_error():
    with (
        patch(
            "app.core.taskiq_result.broker.result_backend.is_result_ready",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.core.taskiq_result.broker.result_backend.get_result",
            AsyncMock(side_effect=ResultGetError()),
        ),
    ):
        response = await build_task_status_response("task-5")

    assert response["task_id"] == "task-5"
    assert response["state"] == "FAILURE"
    assert response["ready"] is True
    assert response["is_ready"] is True
    assert "Cannot get result for the task" in response["error"]
