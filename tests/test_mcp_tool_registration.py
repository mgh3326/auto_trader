"""
Tests for MCP tool registration and removal verification.

This module tests:
- DCA tools have been removed from build_tools
- retired Paperclip / weekend crypto MCP tools are not registered
- DCA models no longer exported from models package
- DCA helper functions removed from market_data_indicators
- recommend_stocks tool is properly registered
"""

import pytest

from app.core.config import settings
from app.mcp_server.tooling import market_data_indicators
from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_dca_tools_removed_from_build_tools() -> None:
    """Verify DCA tools have been removed from tool registry."""
    tools = build_tools()

    assert "create_dca_plan" not in tools
    assert "get_dca_status" not in tools


@pytest.mark.asyncio
async def test_retired_mcp_tools_removed_from_build_tools() -> None:
    """Verify retired Paperclip and weekend crypto tools are not registered."""
    tools = build_tools()

    assert "post_paperclip_comment" not in tools
    assert "weekend_crypto_paper_cycle_run" not in tools


def test_models_package_no_longer_exports_dca() -> None:
    """Verify DCA models are no longer exported from app.models."""
    import app.models as models

    assert not hasattr(models, "DcaPlan")
    assert not hasattr(models, "DcaPlanStep")


def test_compute_dca_price_levels_helper_removed() -> None:
    """Verify DCA price levels helper has been removed from indicators module."""
    assert not hasattr(market_data_indicators, "_compute_dca_price_levels")


@pytest.mark.asyncio
async def test_recommend_stocks_removed_from_build_tools() -> None:
    """ROB-359: recommend_stocks is registry-hidden (deprecated/parked).

    The MCP tool surface no longer exposes recommend_stocks so agents cannot
    invoke it as a new-buy basis; screen_stocks is the single candidate-
    discovery entrypoint. The recommend_stocks_impl implementation is retained
    in app.mcp_server.tooling.analysis_tool_handlers for future re-use (e.g. a
    narrow build_buy_plan tool).
    """
    tools = build_tools()

    assert "recommend_stocks" not in tools
    # Primary discovery entrypoint must remain.
    assert "screen_stocks" in tools


@pytest.mark.asyncio
async def test_recommend_stocks_removal_leaves_order_surface_untouched() -> None:
    """Removing the read-only recommend_stocks tool must not alter the
    broker/order tool surface (no order/watch/order-intent side effects)."""
    tools = build_tools()

    # Default profile still exposes its order surface unchanged.
    for order_tool in ("place_order", "cancel_order", "modify_order"):
        assert order_tool in tools
    assert "recommend_stocks" not in tools


@pytest.mark.asyncio
async def test_rob488_immediate_deprecated_tools_removed_from_default_surface() -> None:
    """ROB-488: dead/no-op/footgun tools remain absent from the default surface."""
    tools = build_tools()

    retired = {
        "get_asset_profile",
        "set_asset_profile",
        "get_tier_rule_params",
        "set_tier_rule_params",
        "get_market_filters",
        "set_market_filter",
        "delete_asset_profile",
        "simulate_avg_cost",
        "investment_snapshot_bundle_ensure",
        "investment_snapshot_refresh_request",
    }

    assert retired.isdisjoint(tools)
    assert "get_holdings" in tools
    assert "investment_snapshot_bundle_get" not in tools  # flag remains default-off


@pytest.mark.asyncio
async def test_snapshot_report_generator_tools_are_flag_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-488: disabled generator tools should be absent, not registered no-ops."""
    gated = {
        "investment_report_generate_from_bundle",
        "investment_report_prepare_bundle",
        "investment_report_get_hermes_context",
        "investment_report_create_from_hermes_composition",
        "investment_stage_artifacts_ingest_from_hermes",
        "investment_report_prepare_intraday_context",
    }

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", False, raising=False
    )
    disabled_tools = build_tools()
    assert gated.isdisjoint(disabled_tools)
    assert "investment_report_create" in disabled_tools

    monkeypatch.setattr(
        settings, "SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED", True, raising=False
    )
    enabled_tools = build_tools()
    assert gated <= set(enabled_tools)


@pytest.mark.asyncio
async def test_analysis_bundle_tools_are_default_off_and_flag_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gated = {"analysis_bundle_create", "analysis_bundle_get"}
    monkeypatch.setattr(
        settings, "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED", False, raising=False
    )
    assert gated.isdisjoint(build_tools())

    monkeypatch.setattr(
        settings, "ANALYSIS_SNAPSHOT_BUNDLES_MCP_ENABLED", True, raising=False
    )
    assert gated <= set(build_tools())


@pytest.mark.asyncio
async def test_get_portfolio_allocation_registered_in_default_surface() -> None:
    tools = build_tools()

    assert "get_portfolio_allocation" in tools
