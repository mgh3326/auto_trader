from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.jobs import upbit_symbol_universe as upbit_symbol_universe_job
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.services import upbit_symbol_universe_service
from app.tasks import upbit_symbol_universe_tasks
from scripts import sync_upbit_symbol_universe


@pytest.mark.asyncio
async def test_build_snapshot_skips_invalid_rows(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_service,
        "_fetch_upbit_market_all",
        AsyncMock(
            return_value=[
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "english_name": "Bitcoin",
                    "market_warning": "NONE",
                },
                {
                    "market": "INVALID",
                    "korean_name": "잘못된",
                    "english_name": "Invalid",
                },
                {
                    "market": "KRW-",
                    "korean_name": "누락",
                    "english_name": "Missing",
                },
                {
                    "market": "KRW-ETH",
                    "korean_name": "",
                    "english_name": "Ethereum",
                },
                {
                    "market": "KRW-XRP",
                    "korean_name": "리플",
                    "english_name": "Ripple",
                    "market_warning": "CAUTION",
                },
            ]
        ),
    )

    snapshot = (
        await upbit_symbol_universe_service.build_upbit_symbol_universe_snapshot()
    )

    assert set(snapshot) == {"KRW-BTC", "KRW-XRP"}
    assert snapshot["KRW-BTC"].market == "KRW"
    assert snapshot["KRW-XRP"].market_warning == "CAUTION"


@pytest.mark.asyncio
async def test_build_snapshot_raises_on_empty_source(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_service,
        "_fetch_upbit_market_all",
        AsyncMock(return_value=[]),
    )

    with pytest.raises(ValueError, match="upbit_symbol_universe source is empty"):
        await upbit_symbol_universe_service.build_upbit_symbol_universe_snapshot()


@dataclass
class _FakeResult:
    rows: list[UpbitSymbolUniverse]

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[UpbitSymbolUniverse]):
        self.rows = {row.symbol: row for row in rows}
        self.added: list[UpbitSymbolUniverse] = []
        self.execute_calls = 0
        self.flushed = False

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeResult(list(self.rows.values()))

    def add(self, row: UpbitSymbolUniverse):
        self.added.append(row)
        self.rows[row.symbol] = row

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_sync_service_upsert_deactivate_and_idempotent(monkeypatch):
    snapshot = {
        "KRW-BTC": upbit_symbol_universe_service._UniverseRow(
            symbol="KRW-BTC",
            korean_name="비트코인",
            english_name="Bitcoin",
            market="KRW",
            market_warning="NONE",
        ),
        "KRW-ETH": upbit_symbol_universe_service._UniverseRow(
            symbol="KRW-ETH",
            korean_name="이더리움",
            english_name="Ethereum",
            market="KRW",
            market_warning="CAUTION",
        ),
    }
    monkeypatch.setattr(
        upbit_symbol_universe_service,
        "build_upbit_symbol_universe_snapshot",
        AsyncMock(return_value=snapshot),
    )

    db = _FakeSession(
        [
            UpbitSymbolUniverse(
                symbol="KRW-BTC",
                korean_name="OLD",
                english_name="OLD",
                market="KRW",
                market_warning="NONE",
                is_active=False,
            ),
            UpbitSymbolUniverse(
                symbol="KRW-OLD",
                korean_name="LEGACY",
                english_name="LEGACY",
                market="KRW",
                market_warning="NONE",
                is_active=True,
            ),
        ]
    )

    first = await upbit_symbol_universe_service.sync_upbit_symbol_universe(db=db)
    second = await upbit_symbol_universe_service.sync_upbit_symbol_universe(db=db)

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
    assert db.rows["KRW-BTC"].korean_name == "비트코인"
    assert db.rows["KRW-BTC"].is_active is True
    assert db.rows["KRW-OLD"].is_active is False


@pytest.mark.asyncio
async def test_sync_service_hard_fails_before_db_changes_on_snapshot_error(monkeypatch):
    db = _FakeSession([])
    monkeypatch.setattr(
        upbit_symbol_universe_service,
        "build_upbit_symbol_universe_snapshot",
        AsyncMock(side_effect=ValueError("broken snapshot")),
    )

    with pytest.raises(ValueError, match="broken snapshot"):
        await upbit_symbol_universe_service.sync_upbit_symbol_universe(db=db)

    assert db.execute_calls == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_job_wrapper_returns_success_payload(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_job,
        "sync_upbit_symbol_universe",
        AsyncMock(
            return_value={
                "total": 10,
                "inserted": 3,
                "updated": 5,
                "deactivated": 2,
            }
        ),
    )

    result = await upbit_symbol_universe_job.run_upbit_symbol_universe_sync()

    assert result == {
        "status": "completed",
        "total": 10,
        "inserted": 3,
        "updated": 5,
        "deactivated": 2,
    }


@pytest.mark.asyncio
async def test_job_wrapper_returns_failure_payload(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_job,
        "sync_upbit_symbol_universe",
        AsyncMock(side_effect=RuntimeError("sync failed")),
    )

    result = await upbit_symbol_universe_job.run_upbit_symbol_universe_sync()

    assert result["status"] == "failed"
    assert "sync failed" in str(result["error"])


@pytest.mark.asyncio
async def test_task_returns_success_payload(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_tasks,
        "run_upbit_symbol_universe_sync",
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

    result = await upbit_symbol_universe_tasks.sync_upbit_symbol_universe_task()

    assert result["status"] == "completed"
    assert result["total"] == 10


@pytest.mark.asyncio
async def test_task_returns_failure_payload_on_exception(monkeypatch):
    monkeypatch.setattr(
        upbit_symbol_universe_tasks,
        "run_upbit_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("task failure")),
    )

    result = await upbit_symbol_universe_tasks.sync_upbit_symbol_universe_task()

    assert result["status"] == "failed"
    assert "task failure" in str(result["error"])


@pytest.mark.asyncio
async def test_script_main_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(sync_upbit_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_upbit_symbol_universe,
        "run_upbit_symbol_universe_sync",
        AsyncMock(return_value={"status": "completed", "total": 1}),
    )

    code = await sync_upbit_symbol_universe.main()

    assert code == 0


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_failed_status(monkeypatch):
    monkeypatch.setattr(sync_upbit_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(
        sync_upbit_symbol_universe,
        "run_upbit_symbol_universe_sync",
        AsyncMock(return_value={"status": "failed", "error": "boom"}),
    )

    code = await sync_upbit_symbol_universe.main()

    assert code == 1


@pytest.mark.asyncio
async def test_script_main_returns_nonzero_on_exception(monkeypatch):
    capture_mock = MagicMock()
    monkeypatch.setattr(sync_upbit_symbol_universe, "init_sentry", lambda **_: None)
    monkeypatch.setattr(sync_upbit_symbol_universe, "capture_exception", capture_mock)
    monkeypatch.setattr(
        sync_upbit_symbol_universe,
        "run_upbit_symbol_universe_sync",
        AsyncMock(side_effect=RuntimeError("crash")),
    )

    code = await sync_upbit_symbol_universe.main()

    assert code == 1
    capture_mock.assert_called_once()
