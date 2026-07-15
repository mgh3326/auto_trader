# tests/test_trade_retrospective_tools.py
"""ROB-474 — MCP tool envelopes for trade retrospectives."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.mcp_server.tooling.trade_retrospective_tools import (
    get_retrospective_aggregate,
    get_trade_retrospectives,
    save_trade_retrospective,
    trade_retrospective_pending,
)
from app.models.review import KISLiveOrderLedger, TradeRetrospective

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    await db_session.execute(delete(TradeRetrospective))
    await db_session.execute(delete(KISLiveOrderLedger))
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
async def test_mcp_retry_omits_unsupplied_optional_fields():
    first = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="mcp-presence-retry",
        market="kr",
        lesson="preserve through MCP",
    )
    assert first["success"] is True

    second = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="mcp-presence-retry",
    )

    assert second["success"] is True
    assert second["data"]["market"] == "kr"
    assert second["data"]["lesson"] == "preserve through MCP"


@pytest.mark.asyncio
async def test_mcp_retry_preserves_explicit_null_for_nullable_clear():
    from app.mcp_server.caller_identity import caller_argument_names_var

    first = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="filled",
        correlation_id="mcp-explicit-null",
        lesson="clear through MCP",
    )
    assert first["success"] is True

    token = caller_argument_names_var.set(
        frozenset(
            {
                "symbol",
                "instrument_type",
                "account_mode",
                "outcome",
                "correlation_id",
                "lesson",
            }
        )
    )
    try:
        second = await save_trade_retrospective(
            symbol="005930",
            instrument_type="equity_kr",
            account_mode="kis_mock",
            outcome="filled",
            correlation_id="mcp-explicit-null",
            lesson=None,
        )
    finally:
        caller_argument_names_var.reset(token)

    assert second["success"] is True
    assert second["data"]["lesson"] is None


@pytest.mark.asyncio
async def test_save_validation_error_enumerates_outcomes():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_mock",
        outcome="win",
    )
    assert res["success"] is False
    for value in (
        "filled",
        "partially_filled",
        "unfilled",
        "rejected",
        "cancelled",
    ):
        assert value in res["error"]


def test_save_docstring_enumerates_outcomes():
    doc = save_trade_retrospective.__doc__ or ""
    for value in (
        "filled",
        "partially_filled",
        "unfilled",
        "rejected",
        "cancelled",
    ):
        assert value in doc


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


@pytest.mark.asyncio
async def test_save_retrospective_accepts_fx_fields():
    res = await save_trade_retrospective(
        symbol="AAPL",
        instrument_type="equity_us",
        account_mode="toss_live",
        outcome="filled",
        realized_pnl=60.0,
        buy_fx_rate=1389.33,
        sell_fx_rate=1503.19,
        fx_pnl_krw=22772.0,
        security_pnl_usd=60.0,
        security_pnl_krw=90191.4,
        total_pnl_krw=112963.4,
        fx_rate_source="manual",
        fx_pnl_accuracy="exact",
    )
    assert res["success"] is True
    assert res["data"]["fx_pnl_krw"] == pytest.approx(22772.0)
    assert res["data"]["fx_pnl_accuracy"] == "exact"


def test_tool_names_set_complete():
    from app.mcp_server.tooling.trade_retrospective_registration import (
        TRADE_RETROSPECTIVE_TOOL_NAMES,
    )

    assert TRADE_RETROSPECTIVE_TOOL_NAMES == {
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
        "trade_retrospective_pending",
    }


def test_tools_in_available_surface():
    from app.mcp_server import AVAILABLE_TOOL_NAMES

    for name in (
        "save_trade_retrospective",
        "get_trade_retrospectives",
        "get_retrospective_aggregate",
        "trade_retrospective_pending",
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
        "trade_retrospective_pending",
    }


# ---------------------------------------------------------------------------
# ROB-647 — postmortem forwarding + due-list tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_with_postmortem_envelope():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        trigger_type="fill",
        root_cause_class="analysis",
        next_actions=[{"action": "scale in", "issue_id": "ROB-2"}],
    )
    assert res["success"] is True
    assert res["data"]["trigger_type"] == "fill"
    assert res["data"]["next_actions"][0]["issue_id"] == "ROB-2"


@pytest.mark.asyncio
async def test_save_trigger_without_next_actions_envelope():
    res = await save_trade_retrospective(
        symbol="005930",
        instrument_type="equity_kr",
        account_mode="kis_live",
        outcome="filled",
        trigger_type="fill",
    )
    assert res["success"] is False
    assert "next_actions" in res["error"]


@pytest.mark.asyncio
async def test_pending_tool_envelope(db_session: AsyncSession):
    db_session.add(
        KISLiveOrderLedger(
            trade_date=now_kst(),
            symbol="005930",
            instrument_type="equity_kr",
            side="buy",
            order_type="limit",
            account_mode="kis_live",
            broker="kis",
            status="filled",
            lifecycle_state="filled",
            order_no="K-TOOL-1",
        )
    )
    await db_session.commit()

    res = await trade_retrospective_pending()
    assert res["success"] is True
    refs = {p["suggested_correlation_id"] for p in res["pending"]}
    assert "kis_live:K-TOOL-1" in refs


@pytest.mark.asyncio
async def test_pending_tool_include_cancelled_passthrough(db_session: AsyncSession):
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=now_kst(),
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                account_mode="kis_live",
                broker="kis",
                status="cancelled",
                lifecycle_state="cancelled",
                order_no="K-TOOL-CANCEL",
            )
        ]
    )
    await db_session.commit()

    default = await trade_retrospective_pending()
    assert default["success"] is True
    assert default["include_cancelled"] is False
    default_refs = {p["suggested_correlation_id"] for p in default["pending"]}
    assert "kis_live:K-TOOL-CANCEL" not in default_refs
    assert default["excluded_by_filter"]["cancelled"] >= 1

    opted = await trade_retrospective_pending(include_cancelled=True)
    assert opted["include_cancelled"] is True
    opted_refs = {p["suggested_correlation_id"] for p in opted["pending"]}
    assert "kis_live:K-TOOL-CANCEL" in opted_refs
