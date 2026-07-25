"""ROB-697 M1 — shadow-replay MCP profile (live-tool denial guard).

The ``shadow-replay`` profile is the load-bearing validity guard for the A'
shadow replay harness: a headless replay session must be able to read the
frozen Hermes decision context, the versioned trading policy, and the
advisory lane router — and NOTHING ELSE. No live-fetch tool
(market_data/analysis/news/fundamentals), no order/mutation tool, and none
of the 4 Hermes WRITE tools may be reachable through this profile, or a
replay could leak live market data / persist state and invalidate the
experiment.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_ALLOWED = {
    "investment_report_get_hermes_context",
    "get_trading_policy",
    "route_request",
}
_FORBIDDEN = {
    "get_quote",
    "get_ohlcv",
    "get_orderbook",
    "screen_stocks",
    "get_news",
    "investment_report_create",
    "place_order",
    "kis_mock_place_order",
    "investment_report_create_from_hermes_composition",
}


@pytest.mark.unit
def test_shadow_replay_exposes_only_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registration is unconditional (Step 3 of the brief) — the flag is
    # enforced at call time inside investment_report_get_hermes_context_impl,
    # not at registration time — but set it anyway to prove the allowlist
    # holds regardless of the flag's value.
    monkeypatch.setenv("SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", "true")
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.SHADOW_REPLAY)
    names = set(mcp.tools.keys())
    assert names == _ALLOWED, f"unexpected tools: {names ^ _ALLOWED}"


@pytest.mark.unit
def test_shadow_replay_disjoint_from_forbidden_surface() -> None:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.SHADOW_REPLAY)
    names = set(mcp.tools.keys())
    assert _FORBIDDEN.isdisjoint(names)


@pytest.mark.unit
def test_resolve_shadow_replay() -> None:
    assert resolve_mcp_profile("shadow-replay") is McpProfile.SHADOW_REPLAY
