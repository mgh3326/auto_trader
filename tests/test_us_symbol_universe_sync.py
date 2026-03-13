from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _LookupSession:
    def __init__(self, rows: dict[str, USSymbolUniverse]):
        self._rows = rows

    async def execute(self, stmt):
        wc = getattr(stmt, "whereclause", None)
        if wc is not None:
            symbol: str = getattr(wc.right, "value", "")
            return _ScalarOneResult(self._rows.get(symbol))
        first = next(iter(self._rows), None)
        return _ScalarOneResult(first)


@asynccontextmanager
async def _search_session(
    db_path: Path,
    rows: list[USSymbolUniverse],
) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(USSymbolUniverse.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
        yield session

    await engine.dispose()


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


@pytest.mark.parametrize("raw_symbol", ["BRK/B", "BRK-B"])
def test_parse_cod_rows_canonicalizes_symbol_separators(raw_symbol):
    lines = [_cod_line(raw_symbol, "버크셔B", "Berkshire B")]
    rows, skipped = us_symbol_universe_service._parse_cod_rows(lines, "NYSE")
    assert len(rows) == 1
    assert rows[0].symbol == "BRK.B"
    assert skipped == 0


@pytest.mark.asyncio
async def test_build_snapshot_canonicalizes_symbol_keys(monkeypatch):
    async def fake_download(zip_name: str) -> list[str]:
        if zip_name == "nasmst.cod.zip":
            return [_cod_line("AAPL", "애플", "Apple Inc.")]
        if zip_name == "nysmst.cod.zip":
            return [_cod_line("BRK/B", "버크셔B", "Berkshire Hathaway B")]
        if zip_name == "amsmst.cod.zip":
            return [_cod_line("SPY", "SPDR", "SPDR S&P 500 ETF")]
        raise AssertionError(zip_name)

    monkeypatch.setattr(
        us_symbol_universe_service, "_download_cod_lines", fake_download
    )

    snapshot = await us_symbol_universe_service.build_us_symbol_universe_snapshot()

    assert "BRK.B" in snapshot
    assert "BRK/B" not in snapshot
    assert snapshot["BRK.B"].exchange == "NYSE"
    assert snapshot["BRK.B"].symbol == "BRK.B"


@pytest.mark.asyncio
async def test_sync_deactivates_legacy_non_canonical_symbol(monkeypatch):
    snapshot = {
        "BRK.B": us_symbol_universe_service._UniverseRow(
            symbol="BRK.B",
            exchange="NYSE",
            name_kr="버크셔B",
            name_en="Berkshire B",
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
                symbol="BRK/B",
                exchange="NYSE",
                name_kr="버크셔B",
                name_en="Berkshire B",
                is_active=True,
            ),
        ]
    )

    result = await us_symbol_universe_service.sync_us_symbol_universe(db=db)

    assert result["inserted"] == 1
    assert result["deactivated"] == 1
    assert db.rows["BRK.B"].is_active is True
    assert db.rows["BRK/B"].is_active is False


@pytest.mark.asyncio
@pytest.mark.parametrize("input_symbol", ["BRK.B", "BRK/B", "BRK-B"])
async def test_get_exchange_lookup_accepts_all_symbol_formats(input_symbol):
    row = USSymbolUniverse(
        symbol="BRK.B",
        exchange="NYSE",
        name_kr="버크셔B",
        name_en="Berkshire B",
        is_active=True,
    )
    db = _LookupSession({"BRK.B": row})

    exchange = await us_symbol_universe_service.get_us_exchange_by_symbol(
        input_symbol, db=db
    )
    assert exchange == "NYSE"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["BRK.B", "BRK/B", "BRK-B"])
async def test_search_us_symbols_accepts_all_separator_variants_for_canonical_row(
    tmp_path: Path,
    query: str,
):
    row = USSymbolUniverse(
        symbol="BRK.B",
        exchange="NYSE",
        name_kr="버크셔B",
        name_en="Berkshire Hathaway B",
        is_active=True,
    )

    async with _search_session(tmp_path / "canonical-search.db", [row]) as db:
        results = await us_symbol_universe_service.search_us_symbols(
            query, limit=10, db=db
        )

    assert results == [
        {
            "symbol": "BRK.B",
            "name": "버크셔B",
            "instrument_type": "equity_us",
            "exchange": "NYSE",
            "is_active": True,
        }
    ]


@pytest.mark.asyncio
async def test_search_us_symbols_matches_legacy_separator_row_during_transition(
    tmp_path: Path,
):
    row = USSymbolUniverse(
        symbol="BRK/B",
        exchange="NYSE",
        name_kr="버크셔B",
        name_en="Berkshire Hathaway B",
        is_active=True,
    )

    async with _search_session(tmp_path / "legacy-search.db", [row]) as db:
        results = await us_symbol_universe_service.search_us_symbols(
            "BRK/B", limit=10, db=db
        )

    assert results == [
        {
            "symbol": "BRK/B",
            "name": "버크셔B",
            "instrument_type": "equity_us",
            "exchange": "NYSE",
            "is_active": True,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["버크셔", "Berkshire"])
async def test_search_us_symbols_keeps_name_search_behavior(
    tmp_path: Path,
    query: str,
):
    row = USSymbolUniverse(
        symbol="BRK.B",
        exchange="NYSE",
        name_kr="버크셔 해서웨이 B",
        name_en="Berkshire Hathaway B",
        is_active=True,
    )

    async with _search_session(tmp_path / "name-search.db", [row]) as db:
        results = await us_symbol_universe_service.search_us_symbols(
            query, limit=10, db=db
        )

    assert [result["symbol"] for result in results] == ["BRK.B"]
