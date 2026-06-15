from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.account_routing_registration import (
    ACCOUNT_ROUTING_TOOL_NAMES,
    register_account_routing_tools,
)
from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str):
        assert description

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


def test_account_routing_tool_names_register():
    mcp = DummyMCP()

    register_account_routing_tools(mcp)  # type: ignore[arg-type]

    assert ACCOUNT_ROUTING_TOOL_NAMES == {"suggest_order_account"}
    assert set(mcp.tools) == {"suggest_order_account"}


@pytest.mark.parametrize("profile", list(McpProfile))
def test_suggest_order_account_registered_on_all_read_profiles(profile):
    mcp = DummyMCP()

    register_all_tools(mcp, profile=profile)  # type: ignore[arg-type]

    assert "suggest_order_account" in mcp.tools


@pytest.mark.asyncio
async def test_registered_tool_delegates_to_impl(monkeypatch):
    from app.mcp_server.tooling import account_routing_registration

    calls = []

    async def fake_impl(**kwargs):
        calls.append(kwargs)
        return {"success": True, "recommended_account": "toss"}

    monkeypatch.setattr(
        account_routing_registration,
        "suggest_order_account_impl",
        fake_impl,
    )
    mcp = DummyMCP()
    register_account_routing_tools(mcp)  # type: ignore[arg-type]

    result = await mcp.tools["suggest_order_account"](
        symbol="005930",
        market="kr",
        side="buy",
        quantity=10,
        price=75_000,
    )

    assert result == {"success": True, "recommended_account": "toss"}
    assert calls == [
        {
            "symbol": "005930",
            "market": "kr",
            "side": "buy",
            "quantity": 10,
            "price": 75_000,
            "usd_krw": None,
        }
    ]


@pytest.mark.asyncio
async def test_suggest_order_account_impl_uses_snapshots_and_user_costs(monkeypatch):
    from app.mcp_server.tooling import account_routing_tools as tools

    async def fake_setting(key):
        assert key == "account_costs"
        return {
            "version": 1,
            "routing": {"position_consolidation_threshold_bps": {"kr": 25, "us": 40}},
            "accounts": {
                "kis_domestic": {
                    "broker": "kis",
                    "markets": {"kr": {"commission_bps": 14.7, "fx_spread_bps": 0}},
                },
                "toss": {
                    "broker": "toss",
                    "markets": {"kr": {"commission_bps": 0, "fx_spread_bps": 0}},
                },
            },
        }

    async def fake_capital(**kwargs):
        assert kwargs["include_manual"] is False
        return {
            "accounts": [
                {
                    "account": "kis_domestic",
                    "currency": "KRW",
                    "orderable": 2_000_000.0,
                },
                {"account": "toss", "currency": "KRW", "orderable": 1_000_000.0},
            ],
            "errors": [],
        }

    async def fake_holdings(**kwargs):
        assert kwargs["market"] == "kr"
        assert kwargs["include_current_price"] is False
        return {
            "accounts": [
                {
                    "account": "kis",
                    "broker": "kis",
                    "positions": [{"symbol": "005930", "quantity": 1}],
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr(tools, "get_user_setting", fake_setting)
    monkeypatch.setattr(tools, "get_available_capital_impl", fake_capital)
    monkeypatch.setattr(tools, "_get_holdings_impl", fake_holdings)

    result = await tools.suggest_order_account_impl(
        symbol="005930",
        market="kr",
        side="buy",
        quantity=10,
        price=75_000,
    )

    assert result["success"] is True
    assert result["recommended_account"] == "kis_domestic"
    assert result["position_consolidation"]["foregone_savings_krw"] == pytest.approx(
        1102.5
    )
    assert result["cost_comparison"]["toss"]["total_cost_krw"] == pytest.approx(0)
    assert result["advisory_only"] is True
    assert result["price_source"] == "input"


@pytest.mark.asyncio
async def test_suggest_order_account_impl_rejects_sell_before_quote(monkeypatch):
    from app.mcp_server.tooling import account_routing_tools as tools

    async def fail_quote(_symbol):
        raise AssertionError("quote should not be fetched for unsupported side")

    monkeypatch.setattr(tools, "_fetch_quote_equity_kr", fail_quote)

    with pytest.raises(ValueError, match="buy side only"):
        await tools.suggest_order_account_impl(
            symbol="005930",
            market="kr",
            side="sell",
            quantity=10,
            price=None,
        )


@pytest.mark.asyncio
async def test_suggest_order_account_impl_fetches_us_fx_when_missing(monkeypatch):
    from app.mcp_server.tooling import account_routing_tools as tools

    async def fake_setting(key):
        assert key == "account_costs"
        return None

    async def fake_capital(**kwargs):
        assert kwargs["include_manual"] is False
        return {
            "accounts": [
                {"account": "kis_overseas", "currency": "USD", "orderable": 2_000.0},
                {"account": "toss", "currency": "USD", "orderable": 500.0},
            ],
            "errors": [],
        }

    async def fake_holdings(**kwargs):
        assert kwargs["market"] == "us"
        return {"accounts": [], "errors": []}

    async def fake_usd_krw_rate():
        return 1500.0

    monkeypatch.setattr(tools, "get_user_setting", fake_setting)
    monkeypatch.setattr(tools, "get_available_capital_impl", fake_capital)
    monkeypatch.setattr(tools, "_get_holdings_impl", fake_holdings)
    monkeypatch.setattr(tools, "get_usd_krw_rate", fake_usd_krw_rate)

    result = await tools.suggest_order_account_impl(
        symbol="AAPL",
        market="us",
        side="buy",
        quantity=1,
        price=100,
    )

    assert result["notional"]["usd_krw"] == pytest.approx(1500.0)
    assert set(result["cost_comparison"]) == {"kis_overseas", "toss"}
    assert result["data_quality"] == ["using_default_account_costs_review_required"]
