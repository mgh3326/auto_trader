"""
Tests for MCP tool registration and removal verification.

This module tests:
- DCA tools have been removed from build_tools
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


def test_models_package_no_longer_exports_dca() -> None:
    """Verify DCA models are no longer exported from app.models."""
    import app.models as models

    assert not hasattr(models, "DcaPlan")
    assert not hasattr(models, "DcaPlanStep")


def test_compute_dca_price_levels_helper_removed() -> None:
    """Verify DCA price levels helper has been removed from indicators module."""
    assert not hasattr(market_data_indicators, "_compute_dca_price_levels")


@pytest.mark.asyncio
async def test_recommend_stocks_registration() -> None:
    """Test recommend_stocks tool is registered."""
    tools = build_tools()
    assert "recommend_stocks" in tools
