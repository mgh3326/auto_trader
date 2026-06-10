"""ROB-447: real-FastMCP boot smoke test for tool registration.

The existing MCP tests use DummyMCP/build_tools() whose ``.tools`` is a plain dict —
it silently OVERWRITES duplicate tool names, so it could never catch the
get_market_reports / get_latest_market_brief collision (brief판 shadowing report판).

These tests construct a REAL ``FastMCP(on_duplicate="error")`` and run the actual
``register_all_tools`` for both profiles, so any duplicate tool name fails the test
(matching production main.py, which now also sets on_duplicate="error").
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from app.mcp_server.tooling import register_all_tools
from app.mcp_server.tooling.registry import McpProfile


@pytest.mark.unit
@pytest.mark.parametrize("profile", list(McpProfile))
def test_register_all_tools_no_duplicate_names(profile: McpProfile) -> None:
    # on_duplicate="error" → register_all_tools raises ValueError on ANY duplicate
    # tool name. A clean run proves the registered surface has no name collisions.
    mcp = FastMCP(name="auto_trader-mcp-boot-test", on_duplicate="error")
    register_all_tools(mcp, profile=profile)  # must not raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_market_reports_is_the_brief_surface() -> None:
    # ROB-447: with the report판 registration dropped, get_market_reports must resolve
    # to the brief판 (per-symbol analysis history: params include 'symbol'), NOT the
    # old report판 (params included 'report_type').
    mcp = FastMCP(name="auto_trader-mcp-boot-test", on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.DEFAULT)

    tool = await mcp.get_tool("get_market_reports")
    schema = tool.parameters or {}
    props = set((schema.get("properties") or {}).keys())
    assert "symbol" in props  # brief판 signature
    assert "report_type" not in props  # report판 signature is gone
