from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.us_symbol_universe import USSymbolUniverse
from app.services import us_symbol_universe_service
from app.tasks import us_symbol_universe_tasks
from scripts import sync_us_symbol_universe


def _cod_line(symbol: str, name_kr: str, name_en: str) -> str:
    return "\t".join(["0", "1", "2", "3", symbol, "5", name_kr, name_en])


@pytest.mark.asyncio
async def test_build_snapshot_parses_all_exchanges(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "nasmst.cod.zip":
            return [_cod_line("AAPL", "애플", "Apple Inc.")]
        if zip_name == "nysmst.cod.zip":
            return [_cod_line("TSM", "TSM", "Taiwan Semiconductor")]
        if zip_name == "amsmst.cod.zip":
            return [_cod_line("SPY", "SPDR", "SPDR S&P 500 ETF")]
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        us_symbol_universe_service,
        "_download_cod_lines",
        fake_download,
    )

    snapshot = await us_symbol_universe_service.build_us_symbol_universe_snapshot()

    assert snapshot["AAPL"].exchange == "NASD"
    assert snapshot["TSM"].exchange == "NYSE"
    assert snapshot["SPY"].exchange == "AMEX"


@pytest.mark.asyncio
async def test_build_snapshot_raises_on_empty_source(monkeypatch):
    monkeypatch.setattr(
        us_symbol_universe_service,
        "_download_cod_lines",
        AsyncMock(return_value=[]),
    )

    with pytest.raises(ValueError, match="us_symbol_universe source is empty"):
        await us_symbol_universe_service.build_us_symbol_universe_snapshot()


@dataclass
class _FakeResult:
    rows: list[USSymbolUniverse]

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[USSymbolUniverse]):
        self.rows = {row.symbol: row for row in rows}
        self.added: list[USSymbolUniverse] = []
        self.execute_calls = 0
        self.flushed = False

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeResult(list(self.rows.values()))

    def add(self, row: USSymbolUniverse):
        self.added.append(row)
        self.rows[row.symbol] = row

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_sync_service_upsert_deactivate_and_idempotent(monkeypatch):
    snapshot = {
        "AAPL": us_symbol_universe_service._UniverseRow(
            symbol="AAPL",
            exchange="NASD",
            name_kr="애플",
            name_en="Apple Inc.",
        ),
        "TSM": us_symbol_universe_service._UniverseRow(
            symbol="TSM",
            exchange="NYSE",
            name_kr="TSM",
            name_en="Taiwan Semiconductor",
        ),
    }
    monkeypatch.setattr(
        us_symbol_universe_service,
        "build_us_symbol_universe_snapshot",
        AsyncMock(return_value=snapshot),
    )

    db = _FakeSession(
        [
            USSymbolUniverse(
                symbol="AAPL",
                exchange="NYSE",
                name_kr="OLD",
                name_en="OLD",
                is_active=False,
            ),
            USSymbolUniverse(
                symbol="OLD1",
                exchange="NASD",
                name_kr="LEGACY",
                name_en="LEGACY",
                is_active=True,
            ),
        ]
    )

    first = await us_symbol_universe_service.sync_us_symbol_universe(db=db)
    second = await us_symbol_universe_service.sync_us_symbol_universe(db=db)

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
    assert db.rows["AAPL"].exchange == "NASD"
    assert db.rows["AAPL"].is_active is True
    assert db.rows["OLD1"].is_active is False


@pytest.mark.asyncio
async def test_sync_service_hard_fails_before_db_changes_on_snapshot_error(monkeypatch):
    db = _FakeSession([])
    monkeypatch.setattr(
        us_symbol_universe_service,
        "build_us_symbol_universe_snapshot",
        AsyncMock(side_effect=ValueError("broken snapshot")),
    )

    with pytest.raises(ValueError, match="broken snapshot"):
        await us_symbol_universe_service.sync_us_symbol_universe(db=db)

    assert db.execute_calls == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_task_returns_success_payload(monkeypatch):
    monkeypatch.setattr(
        us_symbol_universe_tasks,
        "run_us_symbol_universe_sync",
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

    result = await us_symbol_universe_tasks.sync_us_symbol_universe_task()

    assert result["status"] == "completed"
    assert result["total"] == 10


@pytest.mark.asyncio
async def test_task_returns_failure_payload_on_exception(monkeypatch):
    monkeypatch.setattr(
        us_symbol_universe_tasks,
        "run_us_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("task failure")),
    )

    result = await us_symbol_universe_tasks.sync_us_symbol_universe_task()

    assert result["status"] == "failed"
    assert "task failure" in str(result["error"])


@pytest.mark.asyncio
async def test_script_main_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(sync_us_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_us_symbol_universe,
        "run_us_symbol_universe_sync",
        AsyncMock(return_value={"status": "completed", "total": 1}),
    )

    code = await sync_us_symbol_universe.main()

    assert code == 0


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_failed_status(monkeypatch):
    monkeypatch.setattr(sync_us_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_us_symbol_universe,
        "run_us_symbol_universe_sync",
        AsyncMock(return_value={"status": "failed", "error": "boom"}),
    )

    code = await sync_us_symbol_universe.main()

    assert code == 1


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_exception(monkeypatch):
    capture_mock = MagicMock()
    monkeypatch.setattr(sync_us_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(sync_us_symbol_universe, "capture_exception", capture_mock)
    monkeypatch.setattr(
        sync_us_symbol_universe,
        "run_us_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("crash")),
    )

    code = await sync_us_symbol_universe.main()

    assert code == 1
    capture_mock.assert_called_once()
