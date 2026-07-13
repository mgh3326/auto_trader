# tests/test_route_request.py
from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.route_request_registration import (
    ROUTE_REQUEST_TOOL_NAMES,
    register_route_request_tools,
)
from tests._mcp_tooling_support import DummyMCP, build_tools


def _route_tool() -> Any:
    mcp = DummyMCP()
    register_route_request_tools(cast(Any, mcp))
    return mcp.tools["route_request"]


def test_tool_name_registered():
    mcp = DummyMCP()
    register_route_request_tools(cast(Any, mcp))
    assert ROUTE_REQUEST_TOOL_NAMES == {"route_request"}
    assert "route_request" in mcp.tools


def test_unknown_intent_returns_error():
    route = _route_tool()
    out = asyncio.run(route(intent="sell_everything", market="kr"))
    assert out["success"] is False
    assert out["error"] == "unknown_intent"


def test_unknown_market_returns_error():
    route = _route_tool()
    out = asyncio.run(route(intent="buy_analysis", market="jp"))
    assert out["success"] is False
    assert out["error"] == "unknown_market"


def test_buy_analysis_echoes_policy_version_and_thresholds():
    route = _route_tool()
    out = asyncio.run(route(intent="buy_analysis", market="kr"))
    assert out["success"] is True
    assert out["lane"] == "buy"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    # buy lane has policy thresholds
    assert out["verdict_thresholds"]["thresholds"]
    assert out["verdict_thresholds"]["lane"] == "buy"


def test_market_brief_has_version_but_empty_thresholds():
    route = _route_tool()
    out = asyncio.run(route(intent="market_brief", market="kr"))
    assert out["success"] is True
    assert out["lane"] == "bootstrap"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    assert out["verdict_thresholds"]["thresholds"] == {}


@pytest.mark.parametrize(
    "intent", ["buy_analysis", "profit_taking", "discovery", "market_brief"]
)
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_deterministic_same_input_same_output(intent, market):
    # ROB-659: exercise the double-call determinism assertion across all 4 intents
    # (and all markets), not just discovery/kr.
    route = _route_tool()
    a = asyncio.run(route(intent=intent, market=market))
    b = asyncio.run(route(intent=intent, market=market))
    assert a == b


def test_missing_intent_returns_deterministic_envelope():
    # ROB-659: a missing arg returns success=false, not a schema crash.
    route = _route_tool()
    out = asyncio.run(route(market="kr"))
    assert out["success"] is False
    assert out["error"] == "missing_intent"


def test_missing_market_returns_deterministic_envelope():
    route = _route_tool()
    out = asyncio.run(route(intent="buy_analysis"))
    assert out["success"] is False
    assert out["error"] == "missing_market"


def test_profile_intersection_crypto_drops_toss_place_order():
    # DEFAULT registers toss_place_order; CRYPTO does not. route_request reads
    # the live-registered surface via mcp.list_tools().
    default_mcp = DummyMCP()
    from app.mcp_server.tooling.registry import register_all_tools

    register_all_tools(cast(Any, default_mcp), profile=McpProfile.DEFAULT)
    default_route = default_mcp.tools["route_request"]
    default_out = asyncio.run(default_route(intent="buy_analysis", market="kr"))
    assert "toss_place_order" in [
        s["tool"] for s in default_out["standard_tool_sequence"]
    ]

    crypto_mcp = DummyMCP()
    register_all_tools(cast(Any, crypto_mcp), profile=McpProfile.CRYPTO)
    crypto_route = crypto_mcp.tools["route_request"]
    crypto_out = asyncio.run(crypto_route(intent="buy_analysis", market="crypto"))
    assert "toss_place_order" not in [
        s["tool"] for s in crypto_out["standard_tool_sequence"]
    ]
    assert "toss_place_order" not in crypto_out["allowed_tools"]
    # ROB-658: the crypto execution tool (generic place_order) must be surfaced,
    # not misclassified as blocked, on the CRYPTO profile.
    assert "place_order" not in crypto_out["blocked_actions"]
    assert "place_order" in crypto_out["allowed_tools"]
    assert "place_order" in [s["tool"] for s in crypto_out["standard_tool_sequence"]]


def test_executing_lane_surfaces_toss_preview_precursor():
    # ROB-659: toss_preview_order mints the approval_hash that toss_place_order
    # requires under TOSS_APPROVAL_HASH_MODE=required. An executing lane must NOT
    # list its own preview precursor in blocked_actions (self-contradiction).
    default_mcp = DummyMCP()
    from app.mcp_server.tooling.registry import register_all_tools

    register_all_tools(cast(Any, default_mcp), profile=McpProfile.DEFAULT)
    route = default_mcp.tools["route_request"]
    for intent in ("buy_analysis", "profit_taking", "discovery"):
        out = asyncio.run(route(intent=intent, market="kr"))
        assert "toss_preview_order" not in out["blocked_actions"], intent
        assert "toss_preview_order" in out["allowed_tools"], intent

    # Bootstrap has no place step, so its preview precursor stays blocked (unchanged).
    brief = asyncio.run(route(intent="market_brief", market="kr"))
    assert brief["lane"] == "bootstrap"
    assert "toss_preview_order" in brief["blocked_actions"]


class TestRouteRequestRegisteredEveryProfile:
    # ROB-760: account_read is a physical account-sync allowlist and must not
    # inherit route/advisory tools.
    @pytest.mark.parametrize(
        "profile",
        [
            p
            for p in McpProfile
            if p not in (McpProfile.ACCOUNT_READ, McpProfile.PAPER_EXECUTION)
        ],
    )
    def test_route_request_present(self, profile: McpProfile) -> None:
        tools = build_tools(profile=profile)
        assert "route_request" in tools
