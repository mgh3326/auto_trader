"""ROB-405 Slice A — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.mock_roundtrip_journal_tasks as task_mod


@pytest.mark.asyncio
async def test_task_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", False
    )
    called = {"n": 0}

    async def _fake_sync(db, **kw):
        called["n"] += 1
        return {"status": "ok", "created": 0, "closed": 0}

    monkeypatch.setattr(task_mod, "sync_mock_roundtrip_journals", _fake_sync)
    result = await task_mod.mock_roundtrip_journal_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_task_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "MOCK_ROUNDTRIP_JOURNAL_BRIDGE_ENABLED", True
    )
    captured = {"n": 0}

    async def _fake_sync(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "created": 2, "closed": 1}

    monkeypatch.setattr(task_mod, "sync_mock_roundtrip_journals", _fake_sync)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.mock_roundtrip_journal_sync()
    assert result["created"] == 2
    assert captured["n"] == 1
