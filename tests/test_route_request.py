from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.core.config import settings
from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.route_request_lanes import (
    ALL_KNOWN_TOOLS,
    DIRECT_BROKER_MUTATION_TOOLS,
)
from app.mcp_server.tooling.route_request_registration import (
    ROUTE_REQUEST_TOOL_NAMES,
    register_route_request_tools,
)
from tests._mcp_tooling_support import DummyMCP, build_tools


def _noop() -> None:
    return None


def _route_tool(registered: set[str] | None = None) -> Any:
    mcp = DummyMCP()
    for name in set(ALL_KNOWN_TOOLS) if registered is None else registered:
        mcp.tools[name] = _noop
    register_route_request_tools(cast(Any, mcp))
    return mcp.tools["route_request"]


def _build_profile_mcp(profile: McpProfile) -> DummyMCP:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=profile)
    return mcp


def _steps(out: dict[str, Any]) -> list[str]:
    return [step["tool"] for step in out["standard_tool_sequence"]]


def test_tool_name_registered():
    mcp = DummyMCP()
    register_route_request_tools(cast(Any, mcp))
    assert ROUTE_REQUEST_TOOL_NAMES == {"route_request"}
    assert "route_request" in mcp.tools


def test_unknown_intent_returns_error():
    out = asyncio.run(_route_tool()(intent="sell_everything", market="kr"))
    assert out["success"] is False
    assert out["error"] == "unknown_intent"


def test_unknown_market_returns_error():
    out = asyncio.run(_route_tool()(intent="buy_analysis", market="jp"))
    assert out["success"] is False
    assert out["error"] == "unknown_market"


def test_missing_intent_returns_deterministic_envelope():
    out = asyncio.run(_route_tool()(market="kr"))
    assert out["success"] is False
    assert out["error"] == "missing_intent"


def test_missing_market_returns_deterministic_envelope():
    out = asyncio.run(_route_tool()(intent="buy_analysis"))
    assert out["success"] is False
    assert out["error"] == "missing_market"


def test_buy_analysis_echoes_policy_and_proposal_contract():
    out = asyncio.run(_route_tool()(intent="buy_analysis", market="kr"))

    assert out["success"] is True
    assert out["lane"] == "buy"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    assert out["verdict_thresholds"]["thresholds"]
    assert out["verdict_thresholds"]["lane"] == "buy"
    assert out["route_contract"]["version"] == "proposal-led-v1"
    assert out["route_contract"]["execution_ready"] is True
    assert out["route_contract"]["human_approval_required"] is True
    assert _steps(out)[-1] == "order_proposal_create"


@pytest.mark.parametrize("market", ["us", "crypto"])
def test_profit_taking_hides_kr_shadow_rule_from_other_markets(market):
    route = _route_tool()
    out = asyncio.run(route(intent="profit_taking", market=market))
    assert out["success"] is True
    rules = out["verdict_thresholds"]["decision_rules"]
    assert "sell.single_share_exit" not in rules


def test_profit_taking_labels_kr_single_share_rule_as_shadow_only():
    out = asyncio.run(_route_tool()(intent="profit_taking", market="kr"))
    rule = out["verdict_thresholds"]["decision_rules"]["sell.single_share_exit"]

    assert rule["activation_state"] == "shadow"
    assert rule["proposal_enabled"] is False
    assert rule["operator_approval_required"] is True
    assert rule["proposal"]["execution"] == "proposal_only"
    assert rule["proposal"]["auto_approve"] is False


def test_market_brief_has_version_but_empty_thresholds():
    out = asyncio.run(_route_tool()(intent="market_brief", market="kr"))

    assert out["success"] is True
    assert out["lane"] == "bootstrap"
    assert set(out["policy_version"]) == {"version", "content_hash"}
    assert out["verdict_thresholds"]["thresholds"] == {}
    assert out["route_contract"]["execution_mode"] == "read_only"


@pytest.mark.parametrize(
    "intent", ["buy_analysis", "profit_taking", "discovery", "market_brief"]
)
@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_deterministic_same_input_same_output(intent: str, market: str):
    route = _route_tool()
    first = asyncio.run(route(intent=intent, market=market))
    second = asyncio.run(route(intent=intent, market=market))
    assert first == second


class _MissingListToolsMCP(DummyMCP):
    list_tools = None


class _SyncRaisingMCP(DummyMCP):
    def list_tools(self):
        raise RuntimeError("sync registry failure")


class _AsyncRaisingMCP(DummyMCP):
    async def list_tools(self):
        raise RuntimeError("async registry failure")


class _EmptyRegistryMCP(DummyMCP):
    def list_tools(self):
        return []


class _NoneRegistryMCP(DummyMCP):
    def list_tools(self):
        return None


class _NonIterableRegistryMCP(DummyMCP):
    def list_tools(self):
        return 42


class _MalformedRegistryMCP(DummyMCP):
    def list_tools(self):
        return [
            object(),
            SimpleNamespace(name=None),
            SimpleNamespace(name=""),
        ]


@pytest.mark.parametrize(
    "mcp_type",
    [
        _MissingListToolsMCP,
        _SyncRaisingMCP,
        _AsyncRaisingMCP,
        _EmptyRegistryMCP,
        _NoneRegistryMCP,
        _NonIterableRegistryMCP,
        _MalformedRegistryMCP,
    ],
)
def test_registry_introspection_failure_is_static_fail_closed(mcp_type):
    mcp = mcp_type()
    register_route_request_tools(cast(Any, mcp))
    out = asyncio.run(mcp.tools["route_request"](intent="buy_analysis", market="kr"))

    assert out["success"] is False
    assert out["error"] == "registry_introspection_unavailable"
    assert out["degraded"] is True
    assert out["standard_tool_sequence"] == []
    assert out["allowed_tools"] == []
    assert set(out["blocked_actions"]) == DIRECT_BROKER_MUTATION_TOOLS
    assert out["blocked_actions_basis"] == "static_fail_closed"
    assert out["route_contract"]["state"] == "degraded"
    assert out["route_contract"]["execution_ready"] is False
    assert out["route_contract"]["missing_required_tools"] == ["order_proposal_create"]


def test_registered_surface_without_proposal_tool_fails_closed():
    registered = set(ALL_KNOWN_TOOLS) - {"order_proposal_create"}
    out = asyncio.run(_route_tool(registered)(intent="profit_taking", market="us"))

    assert out["success"] is False
    assert out["error"] == "required_route_tool_unavailable"
    assert out["route_contract"]["state"] == "degraded"
    assert out["route_contract"]["execution_ready"] is False
    assert out["route_contract"]["missing_required_tools"] == ["order_proposal_create"]
    assert "place_order" not in _steps(out)
    assert DIRECT_BROKER_MUTATION_TOOLS & registered <= set(out["blocked_actions"])


@pytest.mark.parametrize(
    "profile",
    [McpProfile.DEFAULT, McpProfile.TRADINGCODEX_EXECUTION],
)
def test_proposal_enabled_execution_profiles_are_route_ready(
    monkeypatch: pytest.MonkeyPatch,
    profile: McpProfile,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    mcp = _build_profile_mcp(profile)
    out = asyncio.run(mcp.tools["route_request"](intent="buy_analysis", market="kr"))

    assert "order_proposal_create" in mcp.tools
    assert out["success"] is True
    assert out["route_contract"]["execution_ready"] is True
    assert _steps(out)[-1] == "order_proposal_create"
    assert DIRECT_BROKER_MUTATION_TOOLS & mcp.tools.keys() <= set(
        out["blocked_actions"]
    )


def test_proposal_flag_off_default_profile_is_not_route_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", False)
    mcp = _build_profile_mcp(McpProfile.DEFAULT)
    out = asyncio.run(mcp.tools["route_request"](intent="buy_analysis", market="kr"))

    assert "order_proposal_create" not in mcp.tools
    assert out["success"] is False
    assert out["error"] == "required_route_tool_unavailable"
    assert out["route_contract"]["execution_ready"] is False


def test_analysis_readonly_stays_fail_closed_when_proposal_gate_is_on(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    mcp = _build_profile_mcp(McpProfile.ANALYSIS_READONLY)
    out = asyncio.run(mcp.tools["route_request"](intent="profit_taking", market="kr"))

    assert "order_proposal_create" not in mcp.tools
    assert out["success"] is False
    assert out["error"] == "required_route_tool_unavailable"
    assert out["route_contract"]["execution_ready"] is False


def test_crypto_profile_blocks_generic_direct_place_for_proposal_lane(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    mcp = _build_profile_mcp(McpProfile.CRYPTO)
    out = asyncio.run(
        mcp.tools["route_request"](intent="buy_analysis", market="crypto")
    )

    assert out["success"] is True
    assert "place_order" in mcp.tools
    assert "place_order" in out["blocked_actions"]
    assert "place_order" not in out["allowed_tools"]
    assert "place_order" not in _steps(out)


def test_discovery_preview_and_direct_execution_non_regression(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "ORDER_PROPOSALS_ENABLED", True)
    mcp = _build_profile_mcp(McpProfile.DEFAULT)
    route = mcp.tools["route_request"]

    discovery = asyncio.run(route(intent="discovery", market="kr"))
    assert discovery["success"] is True
    assert discovery["route_contract"]["execution_mode"] == "legacy_direct"
    assert "toss_preview_order" in discovery["allowed_tools"]
    assert "toss_place_order" in _steps(discovery)

    for intent in ("buy_analysis", "profit_taking"):
        proposal = asyncio.run(route(intent=intent, market="kr"))
        assert "toss_preview_order" in proposal["blocked_actions"]
        assert "toss_preview_order" not in proposal["allowed_tools"]


class TestRouteRequestRegisteredEveryProfile:
    @pytest.mark.parametrize(
        "profile",
        [
            profile
            for profile in McpProfile
            if profile
            not in (
                McpProfile.ACCOUNT_READ,
                McpProfile.PAPER_EXECUTION,
                McpProfile.ALPACA_PAPER_CLEAN,
            )
        ],
    )
    def test_route_request_present(self, profile: McpProfile) -> None:
        tools = build_tools(profile=profile)
        assert "route_request" in tools
