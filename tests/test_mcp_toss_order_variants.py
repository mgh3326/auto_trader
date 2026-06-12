from __future__ import annotations

import pytest

from app.mcp_server.tooling.orders_toss_variants import (
    TOSS_LIVE_ORDER_TOOL_NAMES,
    register_toss_live_order_tools,
    toss_place_order,
)
from app.services.brokers.toss import TossApiDisabled
from tests._mcp_tooling_support import DummyMCP


def test_all_seven_toss_tools_register():
    mcp = DummyMCP()
    register_toss_live_order_tools(mcp)
    assert set(mcp.tools.keys()) == TOSS_LIVE_ORDER_TOOL_NAMES


@pytest.mark.asyncio
async def test_place_order_fails_closed_when_toss_disabled(monkeypatch):
    # Mock validate_toss_api_config to return missing credentials (or simulator of disabled)
    # Actually, we want to mock it to fail when toss is disabled.
    # We will test validate_toss_api_config returning a missing flag or custom config mocking.

    # We can check how toss_api_enabled is set in settings.
    # Let's say settings.toss_api_enabled is False.
    from app.core.config import settings
    monkeypatch.setattr(settings, "toss_api_enabled", False)

    with pytest.raises(TossApiDisabled):
        await toss_place_order(
            symbol="AAPL",
            side="buy",
            quantity="10",
            price="150.0",
            account_mode="toss_live",
        )


@pytest.mark.asyncio
async def test_toss_tools_reject_wrong_account_mode(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    with pytest.raises(ValueError, match="Toss live tools only support account_mode"):
        await toss_place_order(
            symbol="AAPL",
            side="buy",
            quantity="10",
            price="150.0",
            account_mode="kis_live",
        )
