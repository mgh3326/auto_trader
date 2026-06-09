# tests/test_trade_journal_mock_unblock.py
"""ROB-474 — save_trade_journal must accept account_type='mock'."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.trade_journal_tools import (
    get_trade_journal,
    save_trade_journal,
)
from app.models.trade_journal import TradeJournal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeJournal))
    await db_session.commit()


@pytest.mark.asyncio
async def test_save_mock_journal_succeeds():
    res = await save_trade_journal(
        symbol="005930",
        thesis="mock retro practice",
        side="buy",
        entry_price=50000,
        quantity=10,
        account_type="mock",
    )
    assert res["success"] is True, res
    assert res["data"]["account_type"] == "mock"


@pytest.mark.asyncio
async def test_mock_does_not_require_account():
    res = await save_trade_journal(symbol="005930", thesis="t", account_type="mock")
    assert res["success"] is True, res


@pytest.mark.asyncio
async def test_mock_forbids_paper_trade_id():
    res = await save_trade_journal(
        symbol="005930", thesis="t", account_type="mock", paper_trade_id=7
    )
    assert res["success"] is False
    assert "paper_trade_id" in res["error"]


@pytest.mark.asyncio
async def test_get_default_surfaces_mock():
    await save_trade_journal(symbol="005930", thesis="t", account_type="mock")
    res = await get_trade_journal()  # default account_type must now be None (query all)
    assert res["success"] is True
    assert any(e["account_type"] == "mock" for e in res["entries"]), res
