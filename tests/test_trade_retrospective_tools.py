# tests/test_trade_retrospective_tools.py
"""ROB-474 — MCP tool envelopes for trade retrospectives."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
)
from app.models.review import TradeRetrospective

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.commit()


@pytest.mark.asyncio
async def test_save_success_envelope():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
        realized_pnl=100.0,
        realized_pnl_currency="KRW",
        lesson="ok",
    )
    assert res["success"] is True
    assert res["action"] == "created"
    assert res["data"]["strategy_key"] == "A"


@pytest.mark.asyncio
async def test_save_validation_error_envelope():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="bogus",
    )
    assert res["success"] is False
    assert "outcome" in res["error"]


@pytest.mark.asyncio
async def test_save_missing_symbol_envelope():
    res = await save_trade_retrospective(
        symbol="",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
    )
    assert res["success"] is False
    assert "symbol" in res["error"]


@pytest.mark.asyncio
async def test_save_whitespace_symbol_rejected():
    res = await save_trade_retrospective(
        symbol="   ",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
    )
    assert res["success"] is False
    assert "symbol" in res["error"]


@pytest.mark.asyncio
async def test_get_list_envelope():
    await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
    )
    res = await get_trade_retrospectives(strategy_key="A")
    assert res["success"] is True
    assert res["summary"]["count"] == 1
    assert "entries" in res


@pytest.mark.asyncio
async def test_aggregate_envelope():
    await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        strategy_key="A",
        realized_pnl=100.0,
        realized_pnl_currency="KRW",
    )
    res = await get_retrospective_aggregate(group_by="strategy")
    assert res["success"] is True
    assert "groups" in res
    assert res["groups"][0]["group"] == "A"


def test_tool_names_set_complete():
    from app.mcp_server.tooling.trade_retrospective_registration import (
        TRADE_RETROSPECTIVE_TOOL_NAMES,
    )

    assert TRADE_RETROSPECTIVE_TOOL_NAMES == {
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    }


def test_tools_in_available_surface():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    for name in (
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    ):
        assert name in AVAILABLE_TOOL_NAMES


def test_register_wires_three_tools():
    from app.mcp_server.tooling.trade_retrospective_registration import (
        register_trade_retrospective_tools,
    )

    registered: list[str] = []

    class _FakeMCP:
        def tool(self, *, name, description):
            registered.append(name)

            def _wrap(fn):
                return fn

            return _wrap

    register_trade_retrospective_tools(_FakeMCP())
    assert set(registered) == {
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
    }
