"""ROB-307 PR4 — tests for the env-wired runner + the TaskIQ task gate.

The two-key gate must no-op (building zero clients, touching no DB) unless
BOTH BINANCE_DEMO_SCALPING_ENABLED and BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED
are truthy. Verified without creds/DB/network.
"""

from __future__ import annotations

import pytest

from app.jobs.binance_demo_scalping_runner import run_demo_scalping_tick


def _no_client(monkeypatch) -> None:
    import httpx

    def _boom(*a, **k):
        raise AssertionError("disabled gate must not construct an httpx client")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)


@pytest.mark.asyncio
async def test_disabled_when_scheduler_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", raising=False)
    _no_client(monkeypatch)
    result = await run_demo_scalping_tick()
    assert result["status"] == "disabled"
    assert result["scheduler_enabled"] is False


@pytest.mark.asyncio
async def test_disabled_when_base_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_ENABLED", raising=False)
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", "true")
    _no_client(monkeypatch)
    result = await run_demo_scalping_tick()
    assert result["status"] == "disabled"
    assert result["base_enabled"] is False


@pytest.mark.asyncio
async def test_taskiq_task_is_unscheduled_manual_entrypoint(monkeypatch) -> None:
    # Importable + callable; invoking it on the disabled gate is a safe no-op.
    from app.tasks.binance_demo_scalping_tasks import binance_demo_scalping_tick

    monkeypatch.delenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", raising=False)
    _no_client(monkeypatch)
    result = await binance_demo_scalping_tick()
    assert result["status"] == "disabled"
