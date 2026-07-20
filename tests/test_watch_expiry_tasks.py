from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_watch_expiry_sweep_task_is_env_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.tasks.watch_expiry_tasks import sweep_expired_watches_task

    monkeypatch.setattr(settings, "watch_expire_sweep_enabled", False)
    assert await sweep_expired_watches_task() == {"status": "disabled", "expired": 0}


@pytest.mark.asyncio
async def test_watch_expiry_sweep_task_runs_shared_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.tasks.watch_expiry_tasks import sweep_expired_watches_task

    async def _fake_sweep(*, now):
        assert now.tzinfo is not None
        return {"success": True, "expired_count": 2, "expired_alert_uuids": []}

    monkeypatch.setattr(settings, "watch_expire_sweep_enabled", True)
    monkeypatch.setattr(
        "app.tasks.watch_expiry_tasks.run_expired_watches_sweep", _fake_sweep
    )
    assert await sweep_expired_watches_task() == {"status": "ok", "expired": 2}
