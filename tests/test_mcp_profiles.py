"""Tests for MCP profile-driven tool registration.

Verifies that:
- DEFAULT profile registers legacy ambiguous order tools AND typed variants.
- HERMES_PAPER_KIS profile registers only kis_mock_* order tools; live surface absent.
- resolve_mcp_profile handles None/empty/valid/invalid inputs correctly.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.mcp_server.profiles import McpProfile, resolve_mcp_profile
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.registry import register_all_tools
from tests._mcp_tooling_support import DummyMCP

_LEGACY_ORDER_TOOL_NAMES = ORDER_TOOL_NAMES  # {place_order, cancel_order, ...}


def _build_mcp(profile: McpProfile) -> DummyMCP:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp), profile=profile)
    return mcp


class TestDefaultProfile:
    def test_registers_legacy_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert _LEGACY_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_typed_kis_live_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIS_LIVE_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_typed_kis_mock_variants(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert KIS_MOCK_ORDER_TOOL_NAMES <= mcp.tools.keys()


class TestHermesPaperKisProfile:
    def test_does_not_register_legacy_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        for name in _LEGACY_ORDER_TOOL_NAMES:
            assert name not in mcp.tools, (
                f"hermes-paper-kis must not register legacy tool '{name}'"
            )

    def test_does_not_register_live_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        for name in KIS_LIVE_ORDER_TOOL_NAMES:
            assert name not in mcp.tools, (
                f"hermes-paper-kis must not register live tool '{name}'"
            )

    def test_registers_kis_mock_order_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert KIS_MOCK_ORDER_TOOL_NAMES <= mcp.tools.keys()

    def test_registers_readonly_research_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        # Representative read-only tools that must be present in paper profile
        expected_readonly = {"get_quote", "get_holdings", "get_cash_balance"}
        for name in expected_readonly:
            assert name in mcp.tools, (
                f"hermes-paper-kis must register read-only tool '{name}'"
            )


class TestResolveMcpProfile:
    def test_none_returns_default(self) -> None:
        assert resolve_mcp_profile(None) is McpProfile.DEFAULT

    def test_empty_string_returns_default(self) -> None:
        assert resolve_mcp_profile("") is McpProfile.DEFAULT

    def test_whitespace_only_returns_default(self) -> None:
        assert resolve_mcp_profile("   ") is McpProfile.DEFAULT

    def test_explicit_default(self) -> None:
        assert resolve_mcp_profile("default") is McpProfile.DEFAULT

    def test_hermes_paper_kis(self) -> None:
        assert resolve_mcp_profile("hermes-paper-kis") is McpProfile.HERMES_PAPER_KIS

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("unknown-profile")

    def test_invalid_string_mentions_allowed_values(self) -> None:
        with pytest.raises(ValueError, match="default"):
            resolve_mcp_profile("bogus")
