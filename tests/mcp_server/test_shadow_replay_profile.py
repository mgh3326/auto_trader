"""Exact closed-world shadow-replay profile inventory."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_ALLOWED = {
    "canonical_session_context_read",
    "source_event_observe",
    "emitted_trigger_playbook_read",
    "identical_input_snapshot_read",
    "shadow_report_write",
    "deterministic_replay_compare",
    "raw_difference_artifact_write",
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
}


@pytest.mark.unit
def test_shadow_replay_exposes_only_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shadow-replay profile registers unconditionally; set the report
    # generator flag anyway to prove the allowlist holds regardless of its
    # value.
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
