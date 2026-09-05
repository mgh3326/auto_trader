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
async def test_get_market_reports_is_retired_and_the_brief_surface_remains() -> None:
    # ROB-447 dropped the report판 registration so get_market_reports resolved to
    # the brief판. The 2026-09-03 MCP surface audit then found even that brief판
    # tool was class D and removed it, so the name resolves to nothing at all.
    # get_latest_market_brief -- the tool the brief판 module actually serves -- is
    # class C and stays, which is what keeps the ROB-447 collision impossible.
    mcp = FastMCP(name="auto_trader-mcp-boot-test", on_duplicate="error")
    register_all_tools(mcp, profile=McpProfile.DEFAULT)

    names = {tool.name for tool in await mcp.list_tools()}
    assert "get_market_reports" not in names
    assert "get_latest_market_brief" in names
