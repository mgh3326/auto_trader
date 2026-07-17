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
    assert out["version"] == "2026-07-17.3"
    assert out["content_hash"]
    assert out["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert out["decision_rules"] == {}


@pytest.mark.asyncio
async def test_get_trading_policy_returns_crypto_market_rules_and_stamp():
    out = await get_trading_policy(market="crypto", lane="buy")

    assert out["success"] is True
    assert out["version"] == "2026-07-17.3"
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
async def test_get_trading_policy_returns_crash_day_advisory_with_version_echo():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    assert out["crash_day"]["trigger"]["index_symbol"] == "069500"
    assert out["crash_day"]["trigger"]["index_gap_pct_max"] == -3.0
    assert out["crash_day"]["actions"]["new_entry_hold"] is True
    # advisory keys are echoed with the same version/content_hash stamp as
    # every other section of the response (ROB-932).
    assert out["version"]
    assert out["content_hash"]


@pytest.mark.asyncio
async def test_get_trading_policy_returns_user_stances_advisory_with_version_echo():
    out = await get_trading_policy(market="kr", lane="buy")
    assert out["success"] is True
    stances = {s["id"]: s for s in out["user_stances"]}
    stance = stances["ai-demand-real-value-selective"]
    assert stance["review_date"] == "2026-10-17"
    # advisory keys are echoed with the same version/content_hash stamp as
    # every other section of the response (ROB-948, matching ROB-932).
    assert out["version"]
    assert out["content_hash"]


@pytest.mark.asyncio
async def test_get_trading_policy_returns_us_notional_usd_range_with_one_share_exception():
    out = await get_trading_policy(market="us", lane="buy")
    assert out["success"] is True
    us_range = out["thresholds"]["buy.per_symbol_notional_usd_range"]
    assert us_range["value"] == [150, 450]
    assert us_range["one_share_exception"]["absolute_ceiling_usd"] == 700
    assert us_range["one_share_exception"]["max_deep_rungs"] == 1


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
