from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.brokers.toss.dto import TossWarningInfo
from app.services.toss_warnings_sync_service import sync_toss_warnings


class DummyResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class DummyDB:
    def __init__(self, symbols_in_db=None):
        self.added = []
        self.deleted_stmts = []
        self.symbols_in_db = symbols_in_db or []
        self.commit_called = False

    async def execute(self, stmt):
        import sqlalchemy as sa

        # Identify if this is a select on symbol universe or delete
        if isinstance(stmt, sa.sql.selectable.Select):
            # Resolve symbols query
            rows = [(sym,) for sym in self.symbols_in_db]
            return DummyResult(rows)
        elif isinstance(stmt, sa.sql.dml.Delete):
            self.deleted_stmts.append(stmt)
            # mock deleted count return
            mock_res = MagicMock()
            mock_res.rowcount = 1
            return mock_res
        return DummyResult([])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_called = True


class DummyClient:
    def __init__(self, warnings_map=None, holdings_rows=None):
        self.warnings_map = warnings_map or {}
        self.holdings_rows = holdings_rows or []
        self.calls = []

    async def holdings(self):
        return SimpleNamespace(
            items=[
                SimpleNamespace(symbol=symbol, market_country=market_country)
                for symbol, market_country in self.holdings_rows
            ]
        )

    async def warnings(self, symbol: str) -> list[TossWarningInfo]:
        self.calls.append(symbol)
        if symbol == "FAIL":
            raise RuntimeError("API Sync Error")
        return self.warnings_map.get(symbol, [])


class ScopedDummyDB(DummyDB):
    async def execute(self, stmt):
        stmt_text = str(stmt)
        if "kr_symbol_universe" in stmt_text or "us_symbol_universe" in stmt_text:
            raise AssertionError("warnings sync must not poll the full symbol universe")
        if "investment_watch_alerts" in stmt_text:
            return DummyResult([("000660",), ("005930",)])
        if "manual_holdings" in stmt_text:
            return DummyResult([("035720",)])
        return await super().execute(stmt)


@pytest.mark.asyncio
async def test_sync_toss_warnings_replaces_explicit_symbols() -> None:
    db = DummyDB()
    warnings_map = {
        "005930": [
            TossWarningInfo(
                warning_type="OVERHEATED",
                exchange="KRX",
                start_date="2026-06-12",
                end_date=None,
            )
        ],
        "000660": [],
    }
    client = DummyClient(warnings_map=warnings_map)

    res = await sync_toss_warnings(
        db=db, client=client, market="kr", symbols=["005930", "000660"]
    )

    assert res["market"] == "kr"
    assert res["symbols_processed"] == 2
    assert res["warnings_inserted"] == 1
    assert len(db.added) == 1
    assert db.added[0].symbol == "005930"
    assert db.added[0].warning_type == "OVERHEATED"
    assert len(db.deleted_stmts) == 2
    assert db.commit_called is True
    assert len(res["errors"]) == 0


@pytest.mark.asyncio
async def test_sync_toss_warnings_collects_errors() -> None:
    db = DummyDB()
    client = DummyClient()

    # Pass symbol explicitly, one of which will fail
    res = await sync_toss_warnings(
        db=db, client=client, market="kr", symbols=["005930", "FAIL"]
    )

    assert res["symbols_processed"] == 1
    assert len(res["errors"]) == 1
    assert "FAIL" in res["errors"][0]
    assert db.commit_called is True


@pytest.mark.asyncio
async def test_sync_toss_warnings_defaults_to_holdings_and_watch_symbols() -> None:
    db = ScopedDummyDB()
    client = DummyClient(holdings_rows=[("005930", "KR"), ("AAPL", "US")])

    res = await sync_toss_warnings(db=db, client=client, market="kr")

    assert res["symbols_processed"] == 3
    assert sorted(client.calls) == ["000660", "005930", "035720"]
    assert len(db.deleted_stmts) == 3


@pytest.mark.asyncio
async def test_sync_toss_warnings_does_not_delete_when_warning_date_is_invalid() -> (
    None
):
    db = DummyDB()
    client = DummyClient(
        warnings_map={
            "005930": [
                TossWarningInfo(
                    warning_type="OVERHEATED",
                    exchange="KRX",
                    start_date="not-a-date",
                    end_date=None,
                )
            ]
        }
    )

    res = await sync_toss_warnings(
        db=db, client=client, market="kr", symbols=["005930"]
    )

    assert res["symbols_processed"] == 0
    assert len(res["errors"]) == 1
    assert "005930" in res["errors"][0]
    assert db.deleted_stmts == []
    assert db.commit_called is True
