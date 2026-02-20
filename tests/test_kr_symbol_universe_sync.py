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
async def test_task_returns_success_payload(monkeypatch):
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

    result = await kr_symbol_universe_tasks.sync_kr_symbol_universe_task()

    assert result["status"] == "completed"
    assert result["total"] == 10


@pytest.mark.asyncio
async def test_task_returns_failure_payload_on_exception(monkeypatch):
    monkeypatch.setattr(
        kr_symbol_universe_tasks,
        "run_kr_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("task failure")),
    )

    result = await kr_symbol_universe_tasks.sync_kr_symbol_universe_task()

    assert result["status"] == "failed"
    assert "task failure" in str(result["error"])


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
