"""ROB-307 PR4 — tests for the env-wired runner + the TaskIQ task gate.

The two-key gate must no-op (building zero clients, touching no DB) unless
BOTH BINANCE_DEMO_SCALPING_ENABLED and BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED
are truthy. Verified without creds/DB/network.
"""

from __future__ import annotations

import json

import pytest

from app.jobs.binance_demo_scalping_runner import run_demo_scalping_tick
from app.schemas.validated_run_card import GATE_SCHEMA


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


@pytest.mark.asyncio
async def test_enabled_path_wires_without_import_errors(monkeypatch) -> None:
    # Both gates on → the enabled path executes all imports + wiring. Heavy
    # deps are faked (no creds/DB/network). This would have caught the
    # wrong-package import of DemoScalpingMarketData.
    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping_exec.scheduler as schedmod
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", "true")

    class _Cli:
        async def aclose(self):
            return None

    for cls in (BinanceSpotDemoExecutionClient, BinanceFuturesDemoExecutionClient):
        monkeypatch.setattr(cls, "from_env", classmethod(lambda cls: _Cli()))

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _Sess())

    class _Summary:
        def to_evidence_dict(self):
            return {"status": "ran", "entered_count": 0}

    async def _fake_tick(**kwargs):
        return _Summary()

    monkeypatch.setattr(schedmod, "run_scalping_tick", _fake_tick)

    result = await run_demo_scalping_tick()
    assert result["status"] == "ran"


@pytest.mark.asyncio
async def test_enabled_runner_uses_one_transaction_per_execution(monkeypatch) -> None:
    """One symbol's terminal ledger state is committed before the next claim."""
    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping_exec.executor as execmod
    import app.services.brokers.binance.demo_scalping_exec.scheduler as schedmod
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", "true")

    class _Cli:
        async def aclose(self):
            return None

    for cls in (BinanceSpotDemoExecutionClient, BinanceFuturesDemoExecutionClient):
        monkeypatch.setattr(cls, "from_env", classmethod(lambda cls: _Cli()))

    sessions: list[_Sess] = []

    class _Sess:
        def __init__(self):
            self.commits = 0

        async def __aenter__(self):
            sessions.append(self)
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            self.commits += 1

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", _Sess)

    executor_sessions: list[_Sess] = []

    class _Executor:
        def __init__(self, **kwargs):
            executor_sessions.append(kwargs["session"])

        async def execute_monitored(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(execmod, "DemoScalpingExecutor", _Executor)

    class _Summary:
        def to_evidence_dict(self):
            return {"status": "ran", "entered_count": 2}

    async def _fake_tick(**kwargs):
        await kwargs["executors"]["spot"].execute_monitored(object())
        await kwargs["executors"]["spot"].execute_monitored(object())
        return _Summary()

    monkeypatch.setattr(schedmod, "run_scalping_tick", _fake_tick)

    result = await run_demo_scalping_tick()

    assert result["status"] == "ran"
    assert len(sessions) == 2
    assert executor_sessions == sessions
    assert [session.commits for session in sessions] == [1, 1]


def _arm_enabled_path(monkeypatch) -> list[dict]:
    """Fake the enabled path (creds/DB/network) and capture run_scalping_tick
    kwargs so a test can assert the *applied* confirm value.
    """
    import app.core.db as dbmod
    import app.services.brokers.binance.demo_scalping_exec.scheduler as schedmod
    from app.services.brokers.binance.futures_demo.execution_client import (
        BinanceFuturesDemoExecutionClient,
    )
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    monkeypatch.setenv("BINANCE_DEMO_SCALPING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED", "true")

    class _Cli:
        async def aclose(self):
            return None

    for cls in (BinanceSpotDemoExecutionClient, BinanceFuturesDemoExecutionClient):
        monkeypatch.setattr(cls, "from_env", classmethod(lambda cls: _Cli()))

    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _Sess())

    captured: list[dict] = []

    class _Summary:
        def to_evidence_dict(self):
            return {"status": "ran", "entered_count": 0, "error_count": 0}

    async def _fake_tick(**kwargs):
        captured.append(kwargs)
        return _Summary()

    monkeypatch.setattr(schedmod, "run_scalping_tick", _fake_tick)
    return captured


@pytest.mark.asyncio
async def test_confirm_requested_but_gate_unset_downgrades_to_dry_run(
    monkeypatch,
) -> None:
    captured = _arm_enabled_path(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM", "true")
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH", raising=False)

    result = await run_demo_scalping_tick()

    # The confirm actually passed to the scheduler/executor is downgraded.
    assert captured[0]["confirm"] is False
    # Summary reflects both the request and the applied value + gate reason.
    assert result["confirm_requested"] is True
    assert result["confirm"] is False
    assert result["validated_signal_gate"]["allowed"] is False
    assert result["validated_signal_gate"]["reason"] == "gate_path_unset"


@pytest.mark.asyncio
async def test_confirm_requested_with_valid_gate_applies_confirm(
    monkeypatch, tmp_path
) -> None:
    captured = _arm_enabled_path(monkeypatch)
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"schema": GATE_SCHEMA, "verdict": "validated"}), encoding="utf-8"
    )
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM", "true")
    monkeypatch.setenv("BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH", str(gate))

    result = await run_demo_scalping_tick()

    assert captured[0]["confirm"] is True
    assert result["confirm_requested"] is True
    assert result["confirm"] is True
    assert result["validated_signal_gate"]["allowed"] is True


@pytest.mark.asyncio
async def test_confirm_not_requested_leaves_summary_unchanged(monkeypatch) -> None:
    captured = _arm_enabled_path(monkeypatch)
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM", raising=False)
    monkeypatch.delenv("BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH", raising=False)

    result = await run_demo_scalping_tick()

    assert captured[0]["confirm"] is False
    assert result["confirm"] is False
    # No gate evaluation, no new keys when confirm was never requested.
    assert "validated_signal_gate" not in result
    assert "confirm_requested" not in result
