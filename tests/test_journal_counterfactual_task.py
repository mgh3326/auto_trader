"""ROB-405 Slice C — paused taskiq task gating."""

from __future__ import annotations

import pytest

import app.tasks.journal_counterfactual_tasks as task_mod


@pytest.mark.asyncio
async def test_disabled_when_flag_off(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", False)
    called = {"n": 0}

    async def _fake(db, **kw):
        called["n"] += 1
        return {"status": "ok", "created": 0}

    monkeypatch.setattr(task_mod, "sync_journal_counterfactuals", _fake)
    result = await task_mod.journal_counterfactual_sync()
    assert result["status"] == "disabled"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(task_mod.settings, "JOURNAL_COUNTERFACTUAL_ENABLED", True)
    captured = {"n": 0}

    async def _fake(db, **kw):
        captured["n"] += 1
        return {"status": "ok", "created": 1}

    monkeypatch.setattr(task_mod, "sync_journal_counterfactuals", _fake)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.journal_counterfactual_sync()
    assert result["created"] == 1
    assert captured["n"] == 1
