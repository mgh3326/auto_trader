# tests/mcp_server/tooling/test_kis_live_reconcile_tool.py
import pytest


@pytest.mark.unit
def test_reconcile_tool_name_registered():
    from app.mcp_server.tooling.orders_kis_variants import KIS_LIVE_ORDER_TOOL_NAMES

    assert "kis_live_reconcile_orders" in KIS_LIVE_ORDER_TOOL_NAMES


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reconcile_tool_delegates_to_impl():
    from unittest.mock import AsyncMock, patch
    from app.mcp_server.tooling import orders_kis_variants as v

    with patch(
        "app.mcp_server.tooling.kis_live_ledger.kis_live_reconcile_orders_impl",
        new=AsyncMock(return_value={"success": True, "counts": {}}),
    ) as mock_impl:
        out = await v._reconcile_orders_variant(
            symbol="035420", order_id=None, dry_run=True, limit=100,
            account_mode=None, account_type=None,
        )
    mock_impl.assert_awaited_once()
    assert out["success"] is True
