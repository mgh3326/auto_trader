"""ROB-703 — paper limit-order MCP handler registration + profile test."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling.paper_limit_order_handler import (
    PAPER_LIMIT_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP


@pytest.mark.unit
def test_paper_limit_tools_registered_on_default() -> None:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert PAPER_LIMIT_ORDER_TOOL_NAMES <= set(mcp.tools.keys())


@pytest.mark.unit
def test_paper_limit_tools_absent_on_shadow_replay() -> None:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.SHADOW_REPLAY)
    assert PAPER_LIMIT_ORDER_TOOL_NAMES.isdisjoint(set(mcp.tools.keys()))


@pytest.mark.unit
def test_paper_limit_tools_nameset_matches_handler() -> None:
    """Pin the exact tool name set so the profile-matrix guard stays aligned."""
    assert PAPER_LIMIT_ORDER_TOOL_NAMES == {
        "paper_place_limit_order",
        "paper_reconcile_orders",
        "paper_cancel_pending_order",
        "paper_list_pending_orders",
    }
