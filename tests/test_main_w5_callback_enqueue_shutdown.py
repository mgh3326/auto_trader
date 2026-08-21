"""R33 lifespan ordering for the durable callback enqueue reaper."""

from __future__ import annotations

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lifespan_reaps_callback_enqueue_tasks_before_broker_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.main as main_module

    calls: list[str] = []

    async def _startup() -> None:
        calls.append("broker.startup")

    async def _setup_monitoring() -> None:
        calls.append("monitoring.setup")

    async def _cleanup_monitoring() -> None:
        calls.append("monitoring.cleanup")

    async def _reap() -> None:
        calls.append("enqueue.reap")

    async def _shutdown() -> None:
        calls.append("broker.shutdown")

    monkeypatch.setattr(main_module.broker, "is_worker_process", False, raising=False)
    monkeypatch.setattr(main_module.broker, "startup", _startup)
    monkeypatch.setattr(main_module.broker, "shutdown", _shutdown)
    monkeypatch.setattr(main_module, "setup_monitoring", _setup_monitoring)
    monkeypatch.setattr(main_module, "cleanup_monitoring", _cleanup_monitoring)
    monkeypatch.setattr(main_module, "shutdown_callback_enqueue_tasks", _reap)

    app = main_module.api
    async with app.router.lifespan_context(app):
        pass

    assert calls == [
        "broker.startup",
        "monitoring.setup",
        "monitoring.cleanup",
        "enqueue.reap",
        "broker.shutdown",
    ]
