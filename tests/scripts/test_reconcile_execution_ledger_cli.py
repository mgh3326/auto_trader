# tests/scripts/test_reconcile_execution_ledger_cli.py
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import scripts.reconcile_execution_ledger as cli


def test_parse_args_accepts_explicit_date_window(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_execution_ledger.py",
            "--broker",
            "kis",
            "--start-date",
            "2026-02-01",
            "--end-date",
            "2026-02-08",
            "--max-pages",
            "25",
        ],
    )

    args = cli.parse_args()
    start_at, end_at = cli.resolve_window_args(args)

    assert start_at == datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
    assert end_at == datetime(2026, 2, 8, 23, 59, 59, 999999, tzinfo=UTC)
    assert args.max_pages == 25


@pytest.mark.asyncio
async def test_main_commits_dry_run_audit_row(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_execution_ledger.py",
            "--broker",
            "kis",
            "--dry-run",
        ],
    )

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "ExecutionLedgerRepository", lambda db: db)

    class FakeDiff:
        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"ok": True}

    class FakeReconciler:
        def __init__(self, repository: object) -> None:
            assert repository is session

        async def run(self, broker: str, **kwargs: object) -> FakeDiff:
            assert broker == "kis"
            assert kwargs["dry_run"] is True
            return FakeDiff()

    monkeypatch.setattr(cli, "ExecutionLedgerReconciler", FakeReconciler)

    rc = await cli._main()

    assert rc == 0
    assert '"ok": true' in capsys.readouterr().out
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_commits_dry_run_audit_row_on_reconcile_error(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "reconcile_execution_ledger.py",
            "--broker",
            "upbit",
            "--dry-run",
        ],
    )

    session = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(cli, "ExecutionLedgerRepository", lambda db: db)

    class FakeReconciler:
        def __init__(self, repository: object) -> None:
            assert repository is session

        async def run(self, broker: str, **kwargs: object) -> None:
            assert broker == "upbit"
            assert kwargs["dry_run"] is True
            raise RuntimeError("filled-orders fetch returned errors")

    monkeypatch.setattr(cli, "ExecutionLedgerReconciler", FakeReconciler)

    with pytest.raises(RuntimeError, match="filled-orders fetch returned errors"):
        await cli._main()

    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
