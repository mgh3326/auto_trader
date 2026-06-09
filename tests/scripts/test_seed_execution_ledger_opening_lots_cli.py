# tests/scripts/test_seed_execution_ledger_opening_lots_cli.py
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.seed_execution_ledger_opening_lots as cli
from app.services.execution_ledger.opening_lots import OpeningLotCandidate


def _candidate() -> OpeningLotCandidate:
    return OpeningLotCandidate(
        broker="kis",
        account_mode="live",
        venue="krx",
        instrument_type="equity_kr",
        symbol="005930",
        raw_symbol="005930",
        currency="KRW",
        current_qty=Decimal("10"),
        avg_price=Decimal("70000"),
    )


@pytest.mark.asyncio
async def test_seed_cli_dry_run_rolls_back(monkeypatch):
    session = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        cli, "load_opening_lot_candidates", AsyncMock(return_value=[_candidate()])
    )
    monkeypatch.setattr(
        "app.services.execution_ledger.repository.ExecutionLedgerRepository.net_quantity_by_match_key_since",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        "app.services.execution_ledger.repository.ExecutionLedgerRepository.classify_fill",
        AsyncMock(return_value="inserted"),
    )

    rc = await cli._run(
        brokers=["kis"],
        cutover=datetime(2026, 5, 10, tzinfo=UTC),
        dry_run=True,
    )

    assert rc == 0
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_cli_commit_requires_gate(monkeypatch):
    monkeypatch.setattr(
        cli,
        "settings",
        SimpleNamespace(EXECUTION_LEDGER_COMMIT_ENABLED=False),
    )

    with pytest.raises(RuntimeError, match="EXECUTION_LEDGER_COMMIT_ENABLED"):
        await cli._run(
            brokers=["kis"],
            cutover=datetime(2026, 5, 10, tzinfo=UTC),
            dry_run=False,
        )
