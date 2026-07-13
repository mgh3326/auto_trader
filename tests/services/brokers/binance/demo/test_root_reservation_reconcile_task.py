"""Default-off TaskIQ boundary for Binance Demo reservation reconcile."""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_enabled", "reconcile_enabled"), [(True, False), (False, True)]
)
async def test_reconcile_task_two_key_gate_builds_no_clients(
    monkeypatch, base_enabled: bool, reconcile_enabled: bool
) -> None:
    import app.tasks as task_package
    import app.tasks.binance_demo_root_reservation_reconcile_tasks as taskmod

    assert taskmod in task_package.TASKIQ_TASK_MODULES

    monkeypatch.setattr(taskmod.settings, "BINANCE_DEMO_SCALPING_ENABLED", base_enabled)
    monkeypatch.setattr(
        taskmod.settings,
        "BINANCE_DEMO_RESERVATION_RECONCILE_ENABLED",
        reconcile_enabled,
    )

    def _boom(**_kwargs):
        raise AssertionError("paused task must not enter DB/client runner")

    monkeypatch.setattr(
        taskmod,
        "run_binance_demo_root_reservation_reconciliation_from_env",
        _boom,
    )

    result = await taskmod.binance_demo_root_reservation_reconcile()

    assert result["status"] == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confirm", "expected_dry_run"), [(False, True), (True, False)]
)
async def test_reconcile_task_mutation_requires_confirm_gate(
    monkeypatch, confirm: bool, expected_dry_run: bool
) -> None:
    import app.tasks.binance_demo_root_reservation_reconcile_tasks as taskmod

    monkeypatch.setattr(taskmod.settings, "BINANCE_DEMO_SCALPING_ENABLED", True)
    monkeypatch.setattr(
        taskmod.settings, "BINANCE_DEMO_RESERVATION_RECONCILE_ENABLED", True
    )
    monkeypatch.setattr(
        taskmod.settings, "BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM", confirm
    )
    monkeypatch.setattr(
        taskmod.settings, "BINANCE_DEMO_RESERVATION_RECONCILE_MIN_AGE_SECONDS", 3600
    )

    captured: dict = {}

    async def _fake_reconcile(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "released": 0}

    monkeypatch.setattr(
        taskmod,
        "run_binance_demo_root_reservation_reconciliation_from_env",
        _fake_reconcile,
    )
    now = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(taskmod, "_utcnow", lambda: now)

    result = await taskmod.binance_demo_root_reservation_reconcile()

    assert result["status"] == "ok"
    assert captured["dry_run"] is expected_dry_run
    assert captured["stale_before"] == now - dt.timedelta(hours=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("available_product", ["spot", "usdm_futures"])
async def test_from_env_runner_keeps_invalid_sibling_lane_unavailable(
    monkeypatch, available_product: str
) -> None:
    import app.jobs.binance_demo_root_reservation_reconciliation as jobmod

    class _Client:
        closed = False

        async def aclose(self):
            self.closed = True

    client = _Client()

    def _invalid_lane():
        raise RuntimeError("lane gate or credentials unavailable")

    if available_product == "spot":
        monkeypatch.setattr(
            jobmod.BinanceSpotDemoExecutionClient, "from_env", lambda: client
        )
        monkeypatch.setattr(
            jobmod.BinanceFuturesDemoExecutionClient, "from_env", _invalid_lane
        )
    else:
        monkeypatch.setattr(
            jobmod.BinanceSpotDemoExecutionClient, "from_env", _invalid_lane
        )
        monkeypatch.setattr(
            jobmod.BinanceFuturesDemoExecutionClient, "from_env", lambda: client
        )

    captured: dict = {}

    async def _fake_kernel(_factory, **kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(
        jobmod, "reconcile_binance_demo_root_reservations", _fake_kernel
    )

    result = await jobmod.run_binance_demo_root_reservation_reconciliation_from_env(
        now=dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC),
        stale_before=dt.datetime(2026, 7, 13, 11, 0, tzinfo=dt.UTC),
        dry_run=True,
    )

    assert set(captured["clients"]) == {available_product}
    assert result["client_initialization"][available_product] == "available"
    sibling = "usdm_futures" if available_product == "spot" else "spot"
    assert result["client_initialization"][sibling] == "unavailable"
    assert client.closed is True


@pytest.mark.asyncio
async def test_from_env_runner_attempts_both_client_closes(monkeypatch) -> None:
    import app.jobs.binance_demo_root_reservation_reconciliation as jobmod

    class _Spot:
        async def aclose(self):
            raise RuntimeError("spot close failed")

    class _Futures:
        closed = False

        async def aclose(self):
            self.closed = True

    spot, futures = _Spot(), _Futures()
    monkeypatch.setattr(jobmod.BinanceSpotDemoExecutionClient, "from_env", lambda: spot)
    monkeypatch.setattr(
        jobmod.BinanceFuturesDemoExecutionClient, "from_env", lambda: futures
    )

    async def _fake_kernel(*_args, **_kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(
        jobmod, "reconcile_binance_demo_root_reservations", _fake_kernel
    )

    with pytest.raises(RuntimeError, match="spot close failed"):
        await jobmod.run_binance_demo_root_reservation_reconciliation_from_env(
            now=dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC),
            stale_before=dt.datetime(2026, 7, 13, 11, 0, tzinfo=dt.UTC),
            dry_run=True,
        )

    assert futures.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_from_env_runner_delegates_transaction_ownership_to_kernel(
    monkeypatch, dry_run: bool
) -> None:
    import app.jobs.binance_demo_root_reservation_reconciliation as jobmod

    class _Client:
        closed = False

        async def aclose(self):
            self.closed = True

    spot, futures = _Client(), _Client()
    monkeypatch.setattr(jobmod.BinanceSpotDemoExecutionClient, "from_env", lambda: spot)
    monkeypatch.setattr(
        jobmod.BinanceFuturesDemoExecutionClient, "from_env", lambda: futures
    )

    class _Session:
        commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            self.commits += 1

    session = _Session()
    monkeypatch.setattr(jobmod, "AsyncSessionLocal", lambda: session)
    captured: dict = {}

    async def _fake_kernel(_session, **kwargs):
        captured.update(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(
        jobmod, "reconcile_binance_demo_root_reservations", _fake_kernel
    )
    now = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)
    stale_before = now - dt.timedelta(hours=1)

    result = await jobmod.run_binance_demo_root_reservation_reconciliation_from_env(
        now=now, stale_before=stale_before, dry_run=dry_run
    )

    assert result["status"] == "ok"
    assert captured["dry_run"] is dry_run
    assert captured["stale_before"] == stale_before
    # Discovery and each candidate own independent transactions inside the
    # kernel; the environment wrapper must never add a batch-wide commit.
    assert session.commits == 0
    assert spot.closed is futures.closed is True
