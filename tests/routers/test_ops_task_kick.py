"""Contract tests for the token-only NCP TaskIQ kick surface."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.middleware.auth import AuthMiddleware
from app.routers.ops_task_kick import router
from app.services.ops_task_kick import registry, service

_TOKEN = "ops-task-token"
_TOKEN_HEADER = "X-Ops-Task-Token"
_KICK_PATH = "/trading/api/ops/tasks/build_invest_screener_snapshots/kick"


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, bool, int]] = []
        self.closed = False

    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool:
        self.set_calls.append((name, value, nx, ex))
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        return sum(self.values.pop(name, None) is not None for name in names)

    async def aclose(self) -> None:
        self.closed = True


class _FakeKicker:
    def __init__(self, task: _FakeTask) -> None:
        self.task = task
        self.task_id: str | None = None

    def with_task_id(self, task_id: str) -> _FakeKicker:
        self.task_id = task_id
        return self

    async def kiq(self, **kwargs: Any) -> None:
        assert self.task_id is not None
        self.task.calls.append((self.task_id, kwargs))


class _FakeTask:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def kicker(self) -> _FakeKicker:
        return _FakeKicker(self)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthMiddleware)
    return app


def _configure_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPS_TASK_KICK_TOKEN", _TOKEN, raising=False)
    monkeypatch.setattr(
        settings,
        "OPS_TASK_KICK_TOKEN_HEADER",
        _TOKEN_HEADER,
        raising=False,
    )


def _install_dispatch_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeRedis, _FakeTask]:
    redis = _FakeRedis()
    task = _FakeTask()
    monkeypatch.setattr(service, "_open_redis", lambda: redis)
    monkeypatch.setattr(service.broker, "find_task", lambda _name: task)
    return redis, task


@pytest.mark.asyncio
async def test_unconfigured_ops_token_returns_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "OPS_TASK_KICK_TOKEN", "", raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="https://test"
    ) as client:
        response = await client.post(_KICK_PATH, headers={"Idempotency-Key": "one"})

    assert response.status_code == 403
    assert "not configured" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_missing_or_wrong_token_returns_401_even_with_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_token(monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=_app()),
        base_url="https://test",
        cookies={"session": "would-be-valid-session"},
    ) as client:
        missing = await client.post(
            _KICK_PATH,
            headers={"Idempotency-Key": "one"},
        )
        wrong = await client.post(
            _KICK_PATH,
            headers={"Idempotency-Key": "two", _TOKEN_HEADER: "wrong"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_allowlist_idempotency_and_parameter_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_token(monkeypatch)
    redis, task = _install_dispatch_fakes(monkeypatch)
    headers = {_TOKEN_HEADER: _TOKEN, "Idempotency-Key": "snapshot-run-1"}
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="https://test"
    ) as client:
        unknown = await client.post(
            "/trading/api/ops/tasks/place_order/kick", headers=headers
        )
        invalid = await client.post(_KICK_PATH, headers=headers, json={"market": "jp"})
        first = await client.post(_KICK_PATH, headers=headers, json={"market": "kr"})
        duplicate = await client.post(
            _KICK_PATH, headers=headers, json={"market": "kr"}
        )

    assert unknown.status_code == 404
    assert invalid.status_code == 422
    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert first.json()["task_id"] == duplicate.json()["task_id"]
    assert first.json()["deduplicated"] is False
    assert duplicate.json()["deduplicated"] is True
    assert len(task.calls) == 1
    assert task.calls[0][1]["market"] == "kr"
    assert redis.set_calls[0][2:] == (True, 3_600)
    assert redis.closed is True


@pytest.mark.asyncio
async def test_reconcile_is_dry_run_only_and_apply_parameter_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_token(monkeypatch)
    _redis, task = _install_dispatch_fakes(monkeypatch)
    path = "/trading/api/ops/tasks/kis_live.reconcile_periodic/kick"
    headers = {_TOKEN_HEADER: _TOKEN, "Idempotency-Key": "reconcile-run-1"}
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="https://test"
    ) as client:
        rejected = await client.post(path, headers=headers, json={"apply": True})
        accepted = await client.post(path, headers=headers, json={})

    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert task.calls == [(accepted.json()["task_id"], {"dry_run": True})]
    assert "apply" not in registry.NoParameters.model_fields


def test_registry_is_static_safe_and_matches_registered_tasks() -> None:
    registry.assert_registry_tasks_registered()
    assert all(
        not any(fragment in name for fragment in registry.FORBIDDEN_TASK_NAME_FRAGMENTS)
        for name in registry.TASK_KICK_REGISTRY
    )
    with pytest.raises(ValueError, match="unsafe task name"):
        registry.validate_registry(
            {"place_order": registry.TaskKickSpec(registry.NoParameters)}
        )


@pytest.mark.asyncio
async def test_result_status_is_pending_done_error_or_explicit_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_token(monkeypatch)
    app = _app()
    headers = {_TOKEN_HEADER: _TOKEN}

    class _Backend:
        def __init__(self, ready: bool, is_err: bool = False) -> None:
            self.ready = ready
            self.is_err = is_err

        async def is_result_ready(self, _task_id: str) -> bool:
            return self.ready

        async def get_result(self, _task_id: str, *, with_logs: bool) -> Any:
            assert with_logs is False
            return SimpleNamespace(
                is_err=self.is_err, return_value={"secret": "hidden"}
            )

    monkeypatch.setattr(service.broker, "result_backend", _Backend(False))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        pending = await client.get(
            "/trading/api/ops/tasks/runs/task-1", headers=headers
        )

    monkeypatch.setattr(service.broker, "result_backend", _Backend(True))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        done = await client.get("/trading/api/ops/tasks/runs/task-1", headers=headers)

    monkeypatch.setattr(service.broker, "result_backend", _Backend(True, is_err=True))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        errored = await client.get(
            "/trading/api/ops/tasks/runs/task-1", headers=headers
        )

    monkeypatch.setattr(service.broker, "result_backend", None)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test"
    ) as client:
        unknown = await client.get(
            "/trading/api/ops/tasks/runs/task-1", headers=headers
        )

    assert pending.json() == {"state": "pending"}
    assert done.json() == {
        "state": "done",
        "result": {"kind": "object", "field_count": 1},
    }
    assert errored.json()["state"] == "error"
    assert unknown.json() == {"state": "unknown"}
