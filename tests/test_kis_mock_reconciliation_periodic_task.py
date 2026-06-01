"""ROB-404 — paused taskiq periodic reconcile task."""

from __future__ import annotations

import pytest

import app.tasks.kis_mock_reconciliation_tasks as task_mod


@pytest.mark.asyncio
async def test_periodic_paused_when_flag_off(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "KIS_MOCK_RECONCILE_PERIODIC_ENABLED", False, raising=False
    )
    called = {"n": 0}

    async def _fake_reconcile(*a, **k):
        called["n"] += 1
        return {"success": True}

    monkeypatch.setattr(task_mod, "run_kis_mock_reconciliation", _fake_reconcile)
    result = await task_mod.kis_mock_reconcile_periodic()
    assert result["status"] == "paused"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_periodic_runs_when_enabled(monkeypatch):
    monkeypatch.setattr(
        task_mod.settings, "KIS_MOCK_RECONCILE_PERIODIC_ENABLED", True, raising=False
    )
    captured = {"dry_run": None, "n": 0}

    async def _fake_reconcile(db, *, dry_run=True, **k):
        captured["dry_run"] = dry_run
        captured["n"] += 1
        return {"success": True, "orders_processed": 0}

    monkeypatch.setattr(task_mod, "run_kis_mock_reconciliation", _fake_reconcile)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(task_mod, "AsyncSessionLocal", lambda: _FakeSession())
    result = await task_mod.kis_mock_reconcile_periodic()
    assert result["success"] is True
    assert captured["n"] == 1
    assert captured["dry_run"] is False
