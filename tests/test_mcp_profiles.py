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
from app.mcp_server.tooling.alpaca_paper import ALPACA_PAPER_READONLY_TOOL_NAMES
from app.mcp_server.tooling.alpaca_paper_orders import (
    ALPACA_PAPER_MUTATING_TOOL_NAMES,
)
from app.mcp_server.tooling.alpaca_paper_preview import ALPACA_PAPER_PREVIEW_TOOL_NAMES
from app.mcp_server.tooling.orders_kis_variants import (
    KIS_LIVE_ORDER_TOOL_NAMES,
    KIS_MOCK_ORDER_TOOL_NAMES,
)
from app.mcp_server.tooling.orders_kiwoom_variants import KIWOOM_MOCK_TOOL_NAMES
from app.mcp_server.tooling.orders_registration import ORDER_TOOL_NAMES
from app.mcp_server.tooling.paper_account_registration import PAPER_ACCOUNT_TOOL_NAMES
from app.mcp_server.tooling.paper_analytics_registration import (
    PAPER_ANALYTICS_TOOL_NAMES,
)
from app.mcp_server.tooling.paper_journal_registration import PAPER_JOURNAL_TOOL_NAMES
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.us_dual_paper import US_DUAL_PAPER_TOOL_NAMES
from tests._mcp_tooling_support import DummyMCP

_LEGACY_ORDER_TOOL_NAMES = ORDER_TOOL_NAMES  # {place_order, cancel_order, ...}
_ALPACA_PAPER_TOOL_NAMES = (
    ALPACA_PAPER_READONLY_TOOL_NAMES
    | ALPACA_PAPER_PREVIEW_TOOL_NAMES
    | ALPACA_PAPER_MUTATING_TOOL_NAMES
)
_US_PAPER_TOOL_NAMES = _ALPACA_PAPER_TOOL_NAMES | US_DUAL_PAPER_TOOL_NAMES
_DB_PAPER_TOOL_NAMES = (
    PAPER_ACCOUNT_TOOL_NAMES | PAPER_ANALYTICS_TOOL_NAMES | PAPER_JOURNAL_TOOL_NAMES
)
_CRYPTO_PROFILE_TOOL_NAMES = {
    "get_crypto_profile",
    "get_kimchi_premium",
    "get_funding_rate",
    "get_open_interest",
    "get_long_short_ratio",
    "get_crypto_market_regime",
    "get_crypto_catalysts",
    "get_crypto_order_flow",
    "get_crypto_social",
    "get_upbit_index",
    "get_upbit_altseason",
    "get_crypto_fear_greed",
}


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

    def test_does_not_register_split_profile_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        split_only = (
            _US_PAPER_TOOL_NAMES
            | _DB_PAPER_TOOL_NAMES
            | KIWOOM_MOCK_TOOL_NAMES
            | _CRYPTO_PROFILE_TOOL_NAMES
        )
        assert split_only.isdisjoint(mcp.tools.keys())


class TestAlpacaPaperPreviewProfile:
    def test_preview_tool_registered_us_paper_profile(self) -> None:
        mcp = _build_mcp(McpProfile.US_PAPER)
        assert "alpaca_paper_preview_order" in mcp.tools

    def test_preview_tool_not_registered_default_profile(self) -> None:
        mcp = _build_mcp(McpProfile.DEFAULT)
        assert "alpaca_paper_preview_order" not in mcp.tools

    def test_preview_tool_not_registered_hermes_paper_kis_profile(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert "alpaca_paper_preview_order" not in mcp.tools


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

    def test_does_not_register_alpaca_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.HERMES_PAPER_KIS)
        assert _ALPACA_PAPER_TOOL_NAMES.isdisjoint(mcp.tools.keys())


class TestUsPaperProfile:
    def test_registers_us_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.US_PAPER)
        assert _US_PAPER_TOOL_NAMES <= mcp.tools.keys()


class TestDbPaperProfile:
    def test_registers_db_paper_tools(self) -> None:
        mcp = _build_mcp(McpProfile.DB_PAPER)
        assert _DB_PAPER_TOOL_NAMES <= mcp.tools.keys()


class TestCryptoProfile:
    def test_registers_crypto_profile_tools(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert _CRYPTO_PROFILE_TOOL_NAMES <= mcp.tools.keys()
        assert "get_fear_greed_index" not in mcp.tools

    def test_keeps_generic_research_surface(self) -> None:
        mcp = _build_mcp(McpProfile.CRYPTO)
        assert {"get_quote", "screen_stocks", "get_holdings"} <= mcp.tools.keys()


class TestKiwoomProfile:
    def test_registers_kiwoom_mock_tools(self) -> None:
        mcp = _build_mcp(McpProfile.KIWOOM)
        assert KIWOOM_MOCK_TOOL_NAMES <= mcp.tools.keys()


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

    def test_us_paper(self) -> None:
        assert resolve_mcp_profile("us-paper") is McpProfile.US_PAPER

    def test_db_paper(self) -> None:
        assert resolve_mcp_profile("db-paper") is McpProfile.DB_PAPER

    def test_crypto(self) -> None:
        assert resolve_mcp_profile("crypto") is McpProfile.CRYPTO

    def test_kiwoom(self) -> None:
        assert resolve_mcp_profile("kiwoom") is McpProfile.KIWOOM

    def test_invalid_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown MCP_PROFILE"):
            resolve_mcp_profile("unknown-profile")

    def test_invalid_string_mentions_allowed_values(self) -> None:
        with pytest.raises(ValueError, match="default"):
            resolve_mcp_profile("bogus")
