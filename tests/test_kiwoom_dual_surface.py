from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import research.kr_backfill.collect as collect
from research.kr_backfill.collect import _build_surface_runtime, _prepare_surface_env
from research.kr_backfill.dual_surface import (
    OverlapMismatch,
    SurfaceAuthError,
    SurfaceBackoffExhausted,
    SurfaceContractError,
    SurfaceManifest,
    SurfacePacer,
    compare_overlap_exact,
    validate_surface_rows,
)
from research.kr_backfill.split_assign import (
    assign_surfaces,
    write_dual_surface_assignment,
)


def _row(ticker: str, surface: str, kind: str = "bulk") -> dict[str, str]:
    return {
        "ticker": ticker,
        "surface": surface,
        "assignment_kind": kind,
    }


def test_bulk_symbol_cannot_be_assigned_to_both_surfaces():
    with pytest.raises(SurfaceContractError, match="cross-surface split"):
        validate_surface_rows([_row("005930", "mock"), _row("005930", "live")])


def test_only_explicit_overlap_rows_may_name_both_surfaces():
    rows = [_row("005930", "mock", "overlap"), _row("005930", "live", "overlap")]
    assert validate_surface_rows(rows) == {"005930": ["mock", "live"]}


def test_deterministic_bulk_assignment_has_one_surface_per_symbol():
    assignment = assign_surfaces(["000001", "000002", "000003", "000004"])
    assert set(assignment) == {"000001", "000002", "000003", "000004"}
    assert set(assignment.values()) <= {"mock", "live"}


def test_surface_pacing_is_independent_and_never_recovers():
    mock = SurfacePacer("mock")
    live = SurfacePacer("live")
    assert mock.interval == 2.0
    assert live.interval == 0.5

    live.note_status(429)
    assert live.interval == 1.0
    assert mock.interval == 2.0
    live.note_status(200)
    assert live.interval == 1.0
    live.note_status(429)
    assert live.interval == 2.0
    assert mock.interval == 2.0

    with pytest.raises(SurfaceBackoffExhausted):
        live.note_status(429)
    with pytest.raises(SurfaceAuthError):
        mock.note_status(401)


def test_overlap_is_exact_and_mismatch_is_stop_condition():
    row = {
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 10.0,
        "value": 15.0,
    }
    assert (
        compare_overlap_exact("005930", {1: row}, {1: dict(row)})["cells_compared"] == 6
    )
    changed = dict(row, close=1.5000000001)
    with pytest.raises(OverlapMismatch, match="exact overlap mismatch"):
        compare_overlap_exact("005930", {1: row}, {1: changed})


def test_manifest_seals_surface_field_and_overlap_design():
    payload = SurfaceManifest(
        batch_id="batch",
        assignment_path="split.csv",
        assignment_sha256="0" * 64,
    ).to_dict()
    assert payload["manifest_surface_field"] == "surface"
    assert payload["cross_surface_split_prevented"] is True
    assert payload["overlap_sample"] == {
        "size": 2,
        "every_completed_batches": 50,
        "reason": payload["overlap_sample"]["reason"],
    }
    assert payload["backoff"]["per_surface"] is True
    assert payload["backoff"]["auto_recovery"] is False


def test_split_artifact_records_bulk_owner_and_explicit_overlap(tmp_path):
    top500 = tmp_path / "top500.csv"
    top500.write_text(
        "rank,ticker,market\n1,000001,KOSPI\n2,000002,KOSPI\n3,000003,KOSDAQ\n",
        encoding="utf-8",
    )
    split_path, manifest_path = write_dual_surface_assignment(
        top500=top500,
        out_dir=tmp_path / "split",
        overlap_symbols=("000002",),
    )
    rows = split_path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "rank,ticker,market,assignment_kind,surface"
    assert sum("000002" in row for row in rows) == 2
    assert "cross_surface_split_prevented" in manifest_path.read_text(encoding="utf-8")


def test_mock_only_runtime_never_constructs_live_client():
    dispatched: list[tuple[str, str]] = []

    def mock_factory():
        dispatched.append(("mock", "constructed"))
        return object()

    def live_factory():
        raise AssertionError("live factory must not run for --surface mock")

    raw, clients, pacers = _build_surface_runtime(
        ("mock",), mock_factory=mock_factory, live_factory=live_factory
    )
    assert list(raw) == ["mock"]
    assert list(clients) == ["mock"]
    assert list(pacers) == ["mock"]
    assert dispatched == [("mock", "constructed")]


def test_dual_runtime_uses_one_accounted_pacer_per_surface():
    def factory(surface):
        return lambda: object()

    raw, clients, pacers = _build_surface_runtime(
        ("mock", "live"),
        mock_factory=factory("mock"),
        live_factory=factory("live"),
    )
    assert set(raw) == {"mock", "live"}
    assert set(clients) == set(pacers) == {"mock", "live"}


def test_surface_pacer_snapshot_is_attributable_and_value_redacted():
    pacer = SurfacePacer("live")
    pacer.calls = 7
    pacer.consecutive_429 = 1
    pacer.backoff_level = 1

    snapshot = pacer.snapshot()

    assert snapshot["surface"] == "kiwoom_live"
    assert snapshot["calls"] == 7
    assert snapshot["consecutive_429"] == 1
    assert snapshot["backoff_level"] == 1
    assert "token" not in snapshot


def test_mock_only_env_does_not_require_or_read_live_file(monkeypatch, tmp_path):
    mock_file = tmp_path / ".env.kiwoom-mock"
    mock_file.write_text("KIWOOM_MOCK_ENABLED=true\n", encoding="utf-8")
    missing_live_file = tmp_path / ".env.kiwoom-live"

    previous = os.environ.get("ENV_FILE")
    try:
        _prepare_surface_env(("mock",), mock_file, missing_live_file)
        assert os.environ["ENV_FILE"] == str(mock_file)
    finally:
        if previous is None:
            monkeypatch.delenv("ENV_FILE", raising=False)
        else:
            monkeypatch.setenv("ENV_FILE", previous)


def test_ambient_production_env_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env.prod"))
    with pytest.raises(SurfaceContractError, match="production env file"):
        _prepare_surface_env(("mock",), None, tmp_path / ".env.live")


def test_surface_pacer_serializes_concurrent_waits():
    async def scenario():
        pacer = SurfacePacer("live")
        pacer.interval = 0.01
        pacer._last = time.monotonic()
        started = time.monotonic()
        await asyncio.gather(pacer.wait(), pacer.wait())
        return pacer.calls, time.monotonic() - started

    calls, elapsed = asyncio.run(scenario())
    assert calls == 2
    assert elapsed >= 0.019


@pytest.mark.asyncio
async def test_dual_orchestrator_runs_both_surfaces_with_synthetic_fixture(
    monkeypatch, tmp_path
):
    split = tmp_path / "split.csv"
    split.write_text(
        "ticker,surface,assignment_kind\n"
        "000001,mock,bulk\n"
        "000002,live,bulk\n"
        "000003,mock,overlap\n"
        "000003,live,overlap\n",
        encoding="utf-8",
    )
    job_dir = tmp_path / "job"
    dispatches: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, surface):
            self.surface = surface

        async def aclose(self):
            return None

    class FakeConn:
        async def fetch(self, *_args):
            return []

    class FakePool:
        def __init__(self):
            self.closed = False

        @asynccontextmanager
        async def acquire(self):
            yield FakeConn()

        async def close(self):
            self.closed = True

    pool = FakePool()

    def mock_factory():
        return FakeClient("mock")

    def live_factory():
        return FakeClient("live")

    async def fake_fetch(*, client, symbol, **_kwargs):
        dispatches.append((client.client.surface, symbol))
        _kwargs["pacer"].calls += 1
        return {}, {"pages": 1}

    monkeypatch.setattr(collect, "_prepare_surface_env", lambda *args: None)

    async def create_pool(*_args, **_kwargs):
        return pool

    monkeypatch.setattr(collect.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(collect, "dsn", lambda: "postgresql://synthetic")
    monkeypatch.setattr(collect, "fetch_kiwoom_minutes", fake_fetch)
    monkeypatch.setattr(collect, "assert_fetch_window_open", lambda: None)
    monkeypatch.setattr(collect, "now_kst", lambda: collect.datetime(2026, 1, 2))
    from app.services.brokers.kiwoom.client import KiwoomMockClient
    from app.services.brokers.kiwoom.live_market_data import KiwoomLiveReadOnlyClient

    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: mock_factory()),
    )
    monkeypatch.setattr(
        KiwoomLiveReadOnlyClient,
        "from_app_settings",
        classmethod(lambda cls: live_factory()),
    )

    args = SimpleNamespace(
        split_csv=split,
        job_dir=job_dir,
        start_date="2026-01-01",
        end_date="2026-01-02",
        surface=None,
        limit_symbols=None,
        confirm_write=True,
        mock_env_file=None,
        live_env_file=tmp_path / ".env.live",
        baseline_median_ms=2.127,
    )

    result = await collect._run_dual_surface(args)

    assert result == 0
    print(
        "SYNTHETIC_DUAL_DISPATCHES",
        {
            surface: sum(item[0] == surface for item in dispatches)
            for surface in ("mock", "live")
        },
        "POOL_CLOSED",
        pool.closed,
    )
    assert sorted(dispatches) == [("live", "000002"), ("mock", "000001")]
    assert pool.closed is True
    assert (job_dir / "events" / "stage_b_dual_surface_summary.json").is_file()


@pytest.mark.asyncio
async def test_dual_orchestrator_closes_pool_when_initialization_fails(
    monkeypatch, tmp_path
):
    split = tmp_path / "split.csv"
    split.write_text(
        "ticker,surface,assignment_kind\n"
        "000001,mock,bulk\n"
        "000002,live,bulk\n"
        "000003,mock,overlap\n"
        "000003,live,overlap\n",
        encoding="utf-8",
    )

    class FakePool:
        closed = False

        async def close(self):
            self.closed = True

    pool = FakePool()
    monkeypatch.setattr(collect, "_prepare_surface_env", lambda *args: None)

    async def create_pool(*_args, **_kwargs):
        return pool

    monkeypatch.setattr(collect.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(collect, "dsn", lambda: "postgresql://synthetic")

    def fail_guard_init(self, *args):
        raise RuntimeError("guard init")

    monkeypatch.setattr(collect.Guard, "__init__", fail_guard_init)
    from app.services.brokers.kiwoom.client import KiwoomMockClient
    from app.services.brokers.kiwoom.live_market_data import KiwoomLiveReadOnlyClient

    monkeypatch.setattr(
        KiwoomMockClient,
        "from_app_settings",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(
        KiwoomLiveReadOnlyClient,
        "from_app_settings",
        classmethod(lambda cls: object()),
    )
    args = SimpleNamespace(
        split_csv=split,
        job_dir=tmp_path / "job",
        start_date="2026-01-01",
        end_date="2026-01-02",
        surface=None,
        limit_symbols=None,
        confirm_write=True,
        mock_env_file=None,
        live_env_file=tmp_path / ".env.live",
        baseline_median_ms=2.127,
    )

    with pytest.raises(RuntimeError, match="guard init"):
        await collect._run_dual_surface(args)
    assert pool.closed is True
