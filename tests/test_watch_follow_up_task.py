"""ROB-405 Slice E — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.watch_follow_up_tasks as task_mod


@pytest.mark.asyncio
async def test_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", False)
    called = {"n": 0}

    async def _fake(db, **kw):
        called["n"] += 1
        return {"status": "ok", "linked": 0}

    monkeypatch.setattr(task_mod, "sync_watch_follow_up_items", _fake)
    result = await task_mod.watch_follow_up_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "WATCH_FOLLOW_UP_LINK_ENABLED", True)
    captured = {"n": 0}

    async def _fake(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "linked": 2}

    monkeypatch.setattr(task_mod, "sync_watch_follow_up_items", _fake)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.watch_follow_up_sync()
    assert result["linked"] == 2
    assert captured["n"] == 1
