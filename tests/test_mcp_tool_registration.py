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
