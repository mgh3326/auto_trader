"""Regression coverage for the 2026-08-04 count-witness retirement."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECT_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "collect.py"


@pytest.fixture
def collect_module() -> ModuleType:
    module_name = "kr_backfill_collect_count_witness_test"
    sys.path.insert(0, str(COLLECT_PATH.parent))
    try:
        spec = importlib.util.spec_from_file_location(module_name, COLLECT_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(COLLECT_PATH.parent))


class _Connection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: object) -> list[object]:
        self.queries.append(query)
        return []


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Log:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write(self, record: dict[str, object]) -> None:
        self.records.append(record)


@pytest.mark.asyncio
async def test_count_witness_is_removed_but_latency_probe_remains(
    collect_module: ModuleType,
) -> None:
    pool = _Pool()
    log = _Log()
    guard = collect_module.Guard(pool, baseline_median_ms=1_000.0, log=log)

    await guard.check("toss")

    assert not hasattr(guard, "witness")
    assert not hasattr(guard, "snapshot_witness")
    assert len(pool.connection.queries) == 5
    assert all(
        "SELECT time, close FROM public.kr_candles_1m" in query
        for query in pool.connection.queries
    )
    assert all("count(" not in query.lower() for query in pool.connection.queries)
    assert [record["event"] for record in log.records] == ["latency_probe"]


@pytest.mark.asyncio
async def test_latency_abort_is_preserved(
    collect_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter([0.0, 0.006] * 5)
    monkeypatch.setattr(collect_module.time, "perf_counter", lambda: next(ticks))
    pool = _Pool()
    log = _Log()
    guard = collect_module.Guard(pool, baseline_median_ms=1.0, log=log)

    with pytest.raises(collect_module.AbortStream, match="query latency"):
        await guard.check("toss")

    assert len(pool.connection.queries) == 5
    assert [record["event"] for record in log.records] == ["latency_probe"]


def test_consecutive_429_abort_is_preserved(collect_module: ModuleType) -> None:
    guard = collect_module.Guard(_Pool(), baseline_median_ms=1.0, log=_Log())

    guard.note_429("kiwoom", True)
    guard.note_429("kiwoom", True)
    with pytest.raises(collect_module.AbortStream, match="3 consecutive 429s"):
        guard.note_429("kiwoom", True)


def test_research_upsert_target_is_unchanged(collect_module: ModuleType) -> None:
    assert collect_module.TARGET_TABLE == "research.kr_candles_1m"
    assert "INSERT INTO research.kr_candles_1m" in collect_module.UPSERT_SQL
    assert "public." not in collect_module.UPSERT_SQL
