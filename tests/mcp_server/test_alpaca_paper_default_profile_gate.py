"""ROB-908 — registry gate for Alpaca paper tools in the DEFAULT profile.

The mock_alpaca operator session runs on the DEFAULT profile (single 8765→8766
server). Alpaca paper read + preview + confirm-gated order + ledger tools are
*physically absent* from DEFAULT unless the operator opts in via
``settings.alpaca_paper_default_tools_enabled`` (default False), mirroring the
ROB-601/ROB-867 kiwoom-mock DEFAULT gate.

Core safety invariant: ``alpaca_paper_automated_submit_order`` (ROB-842
governance deny-list) is NEVER exposed in DEFAULT — even when the flag is on it
stays US_PAPER-only. And US_PAPER remains unchanged regardless of the flag.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile
from app.mcp_server.tooling import registry as registry_mod
from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_automated_orders import (
    ALPACA_PAPER_AUTOMATED_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_orders import ALPACA_PAPER_MUTATING_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_preview import ALPACA_PAPER_PREVIEW_TOOL_NAMES
from app.mcp_server.tooling.market_quote_snapshot_tools import (
    MARKET_QUOTE_SNAPSHOT_TOOL_NAMES,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES
from tests._mcp_tooling_support import DummyMCP

# The full flag-gated DEFAULT surface (20 tools): read + preview + us_dual +
# confirm-gated submit/cancel + ledger reads + market quote snapshot tools.
_DEFAULT_ALPACA_TOOL_NAMES = (
    ALPACA_PAPER_READONLY_TOOL_NAMES
    | ALPACA_PAPER_PREVIEW_TOOL_NAMES
    | US_DUAL_PAPER_TOOL_NAMES
    | ALPACA_PAPER_MUTATING_TOOL_NAMES
    | MARKET_QUOTE_SNAPSHOT_TOOL_NAMES
)


def _default_tools(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> set[str]:
    monkeypatch.setattr(
        registry_mod.settings,
        "alpaca_paper_default_tools_enabled",
        enabled,
        raising=False,
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.DEFAULT)
    return set(mcp.tools.keys())


@pytest.mark.unit
def test_alpaca_paper_tools_absent_when_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _default_tools(monkeypatch, enabled=False)
    leaked = _DEFAULT_ALPACA_TOOL_NAMES & tools
    assert not leaked, (
        f"alpaca paper tools leaked into DEFAULT while gate off: {sorted(leaked)}"
    )


@pytest.mark.unit
def test_alpaca_paper_tools_present_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = _default_tools(monkeypatch, enabled=True)
    missing = _DEFAULT_ALPACA_TOOL_NAMES - tools
    assert not missing, (
        f"alpaca paper tools missing from DEFAULT while gate on: {sorted(missing)}"
    )


@pytest.mark.unit
def test_automated_orders_never_registered_in_default_when_gate_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ROB-842 governance: alpaca_paper_automated_submit_order (and its preview)
    # must NEVER be exposed in DEFAULT — it stays US_PAPER-only even with the flag.
    tools = _default_tools(monkeypatch, enabled=True)
    leaked = ALPACA_PAPER_AUTOMATED_TOOL_NAMES & tools
    assert not leaked, (
        f"automated order tools must not surface in DEFAULT: {sorted(leaked)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("enabled", [False, True])
def test_us_paper_profile_unchanged_regardless_of_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setattr(
        registry_mod.settings,
        "alpaca_paper_default_tools_enabled",
        enabled,
        raising=False,
    )
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=McpProfile.US_PAPER)
    tools = set(mcp.tools.keys())
    expected = _DEFAULT_ALPACA_TOOL_NAMES | ALPACA_PAPER_AUTOMATED_TOOL_NAMES
    missing = expected - tools
    assert not missing, f"US_PAPER surface regressed: {sorted(missing)}"
