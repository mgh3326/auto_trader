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
