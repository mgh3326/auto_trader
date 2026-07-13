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
    assert out["version"] == "2026-07-14.1"
    assert out["content_hash"]
    assert out["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert out["decision_rules"] == {}


@pytest.mark.asyncio
async def test_get_trading_policy_returns_crypto_market_rules_and_stamp():
    out = await get_trading_policy(market="crypto", lane="buy")

    assert out["success"] is True
    assert out["version"] == "2026-07-14.1"
    assert len(out["content_hash"]) == 12
    assert out["market_rules"]["recovery_gate"]["min_conditions_met"] == 2
    assert out["market_rules"]["no_chasing"]["daily_change_pct_threshold"] is None


@pytest.mark.asyncio
async def test_get_trading_policy_returns_sell_trim_preplace_rule():
    out = await get_trading_policy(market="kr", lane="sell")
    assert out["success"] is True
    rule = out["decision_rules"]["sell.trim_preplace"]
    assert rule["tiers"][0]["action"] == "preplace_small_trim_ladder"
    assert rule["tiers"][1]["conditions"]["resistance_near_pct_max"] == 2
    assert rule["tiers"][2]["action"] == "register_watch"


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
