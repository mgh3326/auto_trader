from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services import kr_symbol_universe_service
from app.tasks import kr_symbol_universe_tasks
from scripts import sync_kr_symbol_universe


def _make_mst_line(symbol: str, name: str, suffix_length: int) -> str:
    return f"{symbol:<9}{'STD000000000':<12}{name}" + ("0" * suffix_length)


@pytest.mark.asyncio
async def test_build_snapshot_marks_nxt_eligibility(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "kospi_code.mst.zip":
            return [_make_mst_line("005930", "삼성전자", 228)]
        if zip_name == "kosdaq_code.mst.zip":
            return [_make_mst_line("035420", "NAVER", 222)]
        if zip_name == "nxt_kospi_code.mst.zip":
            return [_make_mst_line("005930", "삼성전자", 228)]
        if zip_name == "nxt_kosdaq_code.mst.zip":
            return []
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_download_mst_lines",
        fake_download,
    )

    snapshot = await kr_symbol_universe_service.build_kr_symbol_universe_snapshot()

    assert snapshot["005930"].exchange == "KOSPI"
    assert snapshot["005930"].nxt_eligible is True
    assert snapshot["035420"].exchange == "KOSDAQ"
    assert snapshot["035420"].nxt_eligible is False


@pytest.mark.asyncio
async def test_build_snapshot_skips_invalid_base_symbol_rows(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "kospi_code.mst.zip":
            return [
                _make_mst_line("F70100022", "특수코드", 228),
                _make_mst_line("005930", "삼성전자", 228),
            ]
        if zip_name == "kosdaq_code.mst.zip":
            return []
        if zip_name == "nxt_kospi_code.mst.zip":
            return [_make_mst_line("005930", "삼성전자", 228)]
        if zip_name == "nxt_kosdaq_code.mst.zip":
            return []
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_download_mst_lines",
        fake_download,
    )

    snapshot = await kr_symbol_universe_service.build_kr_symbol_universe_snapshot()

    assert set(snapshot) == {"005930"}
    assert snapshot["005930"].nxt_eligible is True


@pytest.mark.asyncio
async def test_build_snapshot_skips_invalid_nxt_symbol_rows(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "kospi_code.mst.zip":
            return [_make_mst_line("005930", "삼성전자", 228)]
        if zip_name == "kosdaq_code.mst.zip":
            return []
        if zip_name == "nxt_kospi_code.mst.zip":
            return [
                _make_mst_line("F70100022", "특수코드", 228),
                _make_mst_line("005930", "삼성전자", 228),
            ]
        if zip_name == "nxt_kosdaq_code.mst.zip":
            return []
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_download_mst_lines",
        fake_download,
    )

    snapshot = await kr_symbol_universe_service.build_kr_symbol_universe_snapshot()

    assert set(snapshot) == {"005930"}
    assert snapshot["005930"].nxt_eligible is True


@pytest.mark.asyncio
async def test_build_snapshot_raises_when_nxt_symbol_missing_in_base(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "kospi_code.mst.zip":
            return [_make_mst_line("005930", "삼성전자", 228)]
        if zip_name == "kosdaq_code.mst.zip":
            return []
        if zip_name == "nxt_kospi_code.mst.zip":
            return [_make_mst_line("123456", "없는종목", 228)]
        if zip_name == "nxt_kosdaq_code.mst.zip":
            return []
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_download_mst_lines",
        fake_download,
    )

    with pytest.raises(ValueError, match="NXT symbols missing in base universe"):
        await kr_symbol_universe_service.build_kr_symbol_universe_snapshot()


@pytest.mark.asyncio
async def test_build_snapshot_raises_when_base_has_no_valid_symbols(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "kospi_code.mst.zip":
            return [_make_mst_line("F70100022", "특수코드", 228)]
        if zip_name == "kosdaq_code.mst.zip":
            return []
        if zip_name == "nxt_kospi_code.mst.zip":
            return []
        if zip_name == "nxt_kosdaq_code.mst.zip":
            return []
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_download_mst_lines",
        fake_download,
    )

    with pytest.raises(ValueError, match="base universe has no valid symbols"):
        await kr_symbol_universe_service.build_kr_symbol_universe_snapshot()


@dataclass
class _FakeResult:
    rows: list[KRSymbolUniverse]

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[KRSymbolUniverse]):
        self.rows = {row.symbol: row for row in rows}
        self.added: list[KRSymbolUniverse] = []
        self.execute_calls = 0
        self.flushed = False

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeResult(list(self.rows.values()))

    def add(self, row: KRSymbolUniverse):
        self.added.append(row)
        self.rows[row.symbol] = row

    async def flush(self):
        self.flushed = True


@dataclass
class _LookupResult:
    rows: list[KRSymbolUniverse] | None = None
    scalar: object | None = None

    def scalars(self):
        return self

    def all(self):
        return self.rows or []

    def scalar_one_or_none(self):
        return self.scalar


class _LookupSession:
    def __init__(self, results: list[_LookupResult]):
        self._results = list(results)
        self.queries: list[object] = []

    async def execute(self, query):
        self.queries.append(query)
        if not self._results:
            raise AssertionError("No more mocked execute results")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_sync_service_upsert_deactivate_and_idempotent(monkeypatch):
    snapshot = {
        "005930": kr_symbol_universe_service._UniverseRow(
            symbol="005930",
            name="삼성전자",
            exchange="KOSPI",
            nxt_eligible=True,
        ),
        "035420": kr_symbol_universe_service._UniverseRow(
            symbol="035420",
            name="NAVER",
            exchange="KOSDAQ",
            nxt_eligible=False,
        ),
    }
    monkeypatch.setattr(
        kr_symbol_universe_service,
        "build_kr_symbol_universe_snapshot",
        AsyncMock(return_value=snapshot),
    )

    db = _FakeSession(
        [
            KRSymbolUniverse(
                symbol="005930",
                name="OLD",
                exchange="KOSPI",
                nxt_eligible=False,
                is_active=False,
            ),
            KRSymbolUniverse(
                symbol="000001",
                name="LEGACY",
                exchange="KOSPI",
                nxt_eligible=False,
                is_active=True,
            ),
        ]
    )

    first = await kr_symbol_universe_service.sync_kr_symbol_universe(db=db)
    second = await kr_symbol_universe_service.sync_kr_symbol_universe(db=db)

    assert first == {
        "total": 2,
        "inserted": 1,
        "updated": 1,
        "deactivated": 1,
    }
    assert second == {
        "total": 2,
        "inserted": 0,
        "updated": 0,
        "deactivated": 0,
    }
    assert db.flushed is True
    assert db.rows["005930"].is_active is True
    assert db.rows["005930"].nxt_eligible is True
    assert db.rows["000001"].is_active is False


@pytest.mark.asyncio
async def test_sync_service_hard_fails_before_db_changes_on_snapshot_error(monkeypatch):
    db = _FakeSession([])
    monkeypatch.setattr(
        kr_symbol_universe_service,
        "build_kr_symbol_universe_snapshot",
        AsyncMock(side_effect=ValueError("broken snapshot")),
    )

    with pytest.raises(ValueError, match="broken snapshot"):
        await kr_symbol_universe_service.sync_kr_symbol_universe(db=db)

    assert db.execute_calls == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_get_kr_symbol_by_name_returns_symbol_when_active():
    db = _LookupSession(
        [
            _LookupResult(
                rows=[
                    KRSymbolUniverse(
                        symbol="005930",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=True,
                        is_active=True,
                    )
                ]
            )
        ]
    )

    symbol = await kr_symbol_universe_service.get_kr_symbol_by_name(" 삼성전자 ", db=db)

    assert symbol == "005930"


@pytest.mark.asyncio
async def test_get_kr_symbol_by_name_raises_empty_error():
    db = _LookupSession([_LookupResult(rows=[]), _LookupResult(scalar=None)])

    with pytest.raises(kr_symbol_universe_service.KRSymbolUniverseEmptyError):
        await kr_symbol_universe_service.get_kr_symbol_by_name("삼성전자", db=db)


@pytest.mark.asyncio
async def test_get_kr_symbol_by_name_raises_not_registered_error():
    db = _LookupSession([_LookupResult(rows=[]), _LookupResult(scalar="005930")])

    with pytest.raises(kr_symbol_universe_service.KRSymbolNotRegisteredError):
        await kr_symbol_universe_service.get_kr_symbol_by_name("없는종목", db=db)


@pytest.mark.asyncio
async def test_get_kr_symbol_by_name_raises_inactive_error():
    db = _LookupSession(
        [
            _LookupResult(
                rows=[
                    KRSymbolUniverse(
                        symbol="005930",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=True,
                        is_active=False,
                    )
                ]
            )
        ]
    )

    with pytest.raises(kr_symbol_universe_service.KRSymbolInactiveError):
        await kr_symbol_universe_service.get_kr_symbol_by_name("삼성전자", db=db)


@pytest.mark.asyncio
async def test_get_kr_symbol_by_name_raises_ambiguous_error():
    db = _LookupSession(
        [
            _LookupResult(
                rows=[
                    KRSymbolUniverse(
                        symbol="005930",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=True,
                        is_active=True,
                    ),
                    KRSymbolUniverse(
                        symbol="005935",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=False,
                        is_active=True,
                    ),
                ]
            )
        ]
    )

    with pytest.raises(kr_symbol_universe_service.KRSymbolNameAmbiguousError):
        await kr_symbol_universe_service.get_kr_symbol_by_name("삼성전자", db=db)


@pytest.mark.asyncio
async def test_search_kr_symbols_returns_partial_matches():
    db = _LookupSession(
        [
            _LookupResult(
                rows=[
                    KRSymbolUniverse(
                        symbol="005930",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=True,
                        is_active=True,
                    ),
                    KRSymbolUniverse(
                        symbol="035420",
                        name="NAVER",
                        exchange="KOSDAQ",
                        nxt_eligible=False,
                        is_active=True,
                    ),
                ]
            )
        ]
    )

    rows = await kr_symbol_universe_service.search_kr_symbols("0", 20, db=db)

    assert len(rows) == 2
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["instrument_type"] == "equity_kr"
    assert rows[0]["exchange"] == "KOSPI"


@pytest.mark.asyncio
async def test_search_kr_symbols_caps_limit(monkeypatch):
    captured: dict[str, int] = {}

    async def fake_search_impl(db, query, limit):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(
        kr_symbol_universe_service,
        "_search_kr_symbols_impl",
        fake_search_impl,
    )

    rows = await kr_symbol_universe_service.search_kr_symbols("삼성", 500, db=object())

    assert rows == []
    assert captured["limit"] == 100


@pytest.mark.asyncio
async def test_search_kr_symbols_builds_kospi_priority_order_clause():
    db = _LookupSession(
        [
            _LookupResult(
                rows=[
                    KRSymbolUniverse(
                        symbol="005930",
                        name="삼성전자",
                        exchange="KOSPI",
                        nxt_eligible=True,
                        is_active=True,
                    )
                ]
            )
        ]
    )

    await kr_symbol_universe_service.search_kr_symbols("삼성", 20, db=db)

    compiled = str(
        db.queries[0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    assert "case" in compiled
    assert "kospi" in compiled
    assert "kosdaq" in compiled


@pytest.mark.asyncio
async def test_search_kr_symbols_raises_empty_error_when_table_empty():
    db = _LookupSession([_LookupResult(rows=[]), _LookupResult(scalar=None)])

    with pytest.raises(kr_symbol_universe_service.KRSymbolUniverseEmptyError):
        await kr_symbol_universe_service.search_kr_symbols("삼성", 20, db=db)


@pytest.mark.asyncio
async def test_task_returns_success_payload(monkeypatch):
    async def _run_with_task_lock(*, lock_key: str, ttl_seconds: int, coro_factory):
        del lock_key, ttl_seconds
        return await coro_factory()

    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_kr_symbol_universe_sync",
        AsyncMock(
            return_value={
                "status": "completed",
                "total": 10,
                "inserted": 3,
                "updated": 5,
                "deactivated": 2,
            }
        ),
    )
    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_with_task_lock",
        _run_with_task_lock,
    )

    result = await kr_symbol_universe_tasks.sync_kr_symbol_universe_task()

    assert result["status"] == "completed"
    assert result["total"] == 10


@pytest.mark.asyncio
async def test_task_returns_failure_payload_on_exception(monkeypatch):
    async def _run_with_task_lock(*, lock_key: str, ttl_seconds: int, coro_factory):
        del lock_key, ttl_seconds
        return await coro_factory()

    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_kr_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("task failure")),
    )
    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_with_task_lock",
        _run_with_task_lock,
    )

    result = await kr_symbol_universe_tasks.sync_kr_symbol_universe_task()

    assert result["status"] == "failed"
    assert "task failure" in str(result["error"])


@pytest.mark.asyncio
async def test_task_returns_skipped_payload_on_lock_contention(monkeypatch):
    async def _lock_held(*, lock_key: str, ttl_seconds: int, coro_factory):
        del ttl_seconds, coro_factory
        return {
            "status": "skipped",
            "reason": "lock_held",
            "lock_key": lock_key,
        }

    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_kr_symbol_universe_sync",
        AsyncMock(side_effect=AssertionError("task body must not run when lock held")),
    )
    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_with_task_lock",
        _lock_held,
    )

    result = await kr_symbol_universe_tasks.sync_kr_symbol_universe_task()

    assert result == {
        "status": "skipped",
        "reason": "lock_held",
        "lock_key": kr_symbol_universe_tasks.KR_SYMBOL_UNIVERSE_LOCK_KEY,
    }


@pytest.mark.asyncio
async def test_script_main_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(sync_kr_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_kr_symbol_universe,
        "run_kr_symbol_universe_sync",
        AsyncMock(return_value={"status": "completed", "total": 1}),
    )

    code = await sync_kr_symbol_universe.main()

    assert code == 0


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_failed_status(monkeypatch):
    monkeypatch.setattr(sync_kr_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_kr_symbol_universe,
        "run_kr_symbol_universe_sync",
        AsyncMock(return_value={"status": "failed", "error": "boom"}),
    )

    code = await sync_kr_symbol_universe.main()

    assert code == 1


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_exception(monkeypatch):
    capture_mock = MagicMock()
    monkeypatch.setattr(sync_kr_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(sync_kr_symbol_universe, "capture_exception", capture_mock)
    monkeypatch.setattr(
        sync_kr_symbol_universe,
        "run_kr_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("crash")),
    )

    code = await sync_kr_symbol_universe.main()

    assert code == 1
    capture_mock.assert_called_once()
