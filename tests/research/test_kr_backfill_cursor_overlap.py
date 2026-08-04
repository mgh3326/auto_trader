from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COLLECT_PATH = REPOSITORY_ROOT / "research" / "kr_backfill" / "collect.py"


@pytest.fixture
def collect_module() -> ModuleType:
    module_name = "kr_backfill_collect_cursor_overlap_test"
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


def test_filter_page_to_cursor_is_provider_independent(
    collect_module: ModuleType,
) -> None:
    cursor = datetime(2026, 1, 14, 13, 2)
    bars = {
        datetime(2026, 1, 14, 13, 3): {"close": 3.0},
        datetime(2026, 1, 14, 13, 2): {"close": 2.0},
        datetime(2026, 1, 14, 13, 1): {"close": 1.0},
    }

    eligible, overlap = collect_module.filter_page_to_cursor(bars, cursor)

    assert set(eligible) == {
        datetime(2026, 1, 14, 13, 2),
        datetime(2026, 1, 14, 13, 1),
    }
    assert overlap == 1


def test_insert_domain_excludes_nxt_before_cursor_overlap_proof(
    collect_module: ModuleType,
) -> None:
    bars = {
        datetime(2026, 1, 14, 15, 35): {"close": 3.0},
        datetime(2026, 1, 14, 15, 30): {"close": 2.0},
        datetime(2026, 1, 14, 9, 0): {"close": 1.0},
    }

    domain = collect_module.filter_insert_domain(
        bars,
        datetime(2026, 1, 1),
        datetime(2026, 1, 31, 23, 59, 59),
    )
    _, overlap = collect_module.filter_page_to_cursor(
        domain, datetime(2026, 1, 14, 15, 29)
    )

    assert datetime(2026, 1, 14, 15, 35) not in domain
    assert overlap == 1


@pytest.mark.asyncio
async def test_count_existing_bars_is_read_only(
    collect_module: ModuleType,
) -> None:
    class Connection:
        query = ""

        async def fetchval(self, query: str, *_args: object) -> int:
            self.query = query
            return 2

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self) -> Connection:
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Pool:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def acquire(self) -> Acquire:
            return Acquire(self.connection)

    connection = Connection()
    count = await collect_module.count_existing_bars(
        Pool(connection),
        "005930",
        [datetime(2026, 1, 14, 13, 2), datetime(2026, 1, 14, 13, 1)],
    )

    assert count == 2
    assert connection.query.lstrip().startswith("SELECT count(*)")
    assert "research.kr_candles_1m" in connection.query
