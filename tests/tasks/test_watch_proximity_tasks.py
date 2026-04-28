from __future__ import annotations

import inspect

import pytest


class _FakeMonitor:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.closed = False

    async def run(self) -> dict[str, object]:
        return self._result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_watch_proximity_task_uses_monitor_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.watch_proximity_tasks import run_watch_proximity_task

    monitor = _FakeMonitor(result={"crypto": {"notified": 1}})
    monkeypatch.setattr(
        "app.tasks.watch_proximity_tasks.WatchProximityMonitor",
        lambda: monitor,
    )

    result = await run_watch_proximity_task()

    assert result == {"crypto": {"notified": 1}}
    assert monitor.closed is True


def test_watch_proximity_task_imports_no_forbidden_boundaries() -> None:
    import app.tasks.watch_proximity_tasks as module

    source = inspect.getsource(module)
    forbidden = [
        "app.services.orders",
        "kis_trading_service",
        "order_execution",
        "orders_registration",
        "watch_alerts_registration",
        "create_order_intent",
        "submit_order",
        "place_order",
        "register_watch_alert",
    ]
    for token in forbidden:
        assert token not in source
