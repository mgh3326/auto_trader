from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.trading_policy_tools import get_trading_policy
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.asyncio
async def test_get_trading_policy_returns_thresholds_and_version():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    assert out["version"] == "2026-07-02.1"
    assert out["content_hash"]
    assert out["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10


@pytest.mark.asyncio
async def test_get_trading_policy_unknown_key_explicit_error():
    out = await get_trading_policy(market="jp", lane="buy")
    assert out["success"] is False
    assert out["error"] == "unknown_key"
    assert "jp" in out["detail"]


def test_tool_registered_in_default_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert "get_trading_policy" in mcp.tools


def test_tool_registered_in_crypto_profile():
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.CRYPTO)
    assert "get_trading_policy" in mcp.tools
