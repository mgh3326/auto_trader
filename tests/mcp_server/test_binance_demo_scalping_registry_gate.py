"""ROB-841 — registry gate for the Binance Demo scalping submit tool.

The ``binance_demo_scalping_submit_decision`` tool must be *physically absent*
unless the operator opts in via ``settings.binance_demo_scalping_enabled``
(default False). Gate-off means the tool cannot be reached at all — a
defense-in-depth complement to the per-call dry_run + confirm gates.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import registry as registry_mod
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_TOOL = "binance_demo_scalping_submit_decision"


@pytest.mark.unit
def test_tool_absent_when_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod.settings, "binance_demo_scalping_enabled", False, raising=False
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert _TOOL not in set(mcp.tools.keys())


@pytest.mark.unit
def test_tool_present_when_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry_mod.settings, "binance_demo_scalping_enabled", True, raising=False
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    assert _TOOL in set(mcp.tools.keys())
