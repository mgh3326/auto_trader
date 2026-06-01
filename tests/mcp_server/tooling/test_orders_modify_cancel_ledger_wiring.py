# tests/mcp_server/tooling/test_orders_modify_cancel_ledger_wiring.py
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_kr_cancel_marks_ledger_cancelled():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with (
        patch.object(
            mc,
            "_cancel_kis_domestic",
            new=AsyncMock(return_value={"success": True, "order_id": "OID-1"}),
        ),
        patch(
            "app.mcp_server.tooling.kis_live_ledger._mark_ledger_cancelled",
            new=AsyncMock(return_value=1),
        ) as mock_mark,
    ):
        out = await mc.cancel_order_impl(
            "OID-1", symbol="035420", market="kr", is_mock=False
        )
    assert out["success"] is True
    mock_mark.assert_awaited_once_with("OID-1")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_kr_cancel_does_not_touch_live_ledger():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with (
        patch.object(
            mc,
            "_cancel_kis_domestic",
            new=AsyncMock(return_value={"success": True, "order_id": "OID-2"}),
        ),
        patch(
            "app.mcp_server.tooling.kis_live_ledger._mark_ledger_cancelled",
            new=AsyncMock(return_value=1),
        ) as mock_mark,
    ):
        await mc.cancel_order_impl("OID-2", symbol="035420", market="kr", is_mock=True)
    mock_mark.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_live_kr_cancel_does_not_mark_ledger():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with (
        patch.object(
            mc,
            "_cancel_kis_domestic",
            new=AsyncMock(return_value={"success": False, "order_id": "OID-3"}),
        ),
        patch(
            "app.mcp_server.tooling.kis_live_ledger._mark_ledger_cancelled",
            new=AsyncMock(return_value=1),
        ) as mock_mark,
    ):
        await mc.cancel_order_impl("OID-3", symbol="035420", market="kr", is_mock=False)
    mock_mark.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_kr_modify_repoints_ledger():
    from app.mcp_server.tooling import orders_modify_cancel as mc

    with (
        patch.object(
            mc,
            "_modify_kis_domestic",
            new=AsyncMock(
                return_value={"success": True, "order_id": "OLD", "new_order_id": "NEW"}
            ),
        ),
        patch(
            "app.mcp_server.tooling.kis_live_ledger._repoint_ledger_after_modify",
            new=AsyncMock(return_value=1),
        ) as mock_repoint,
    ):
        out = await mc.modify_order_impl(
            "OLD",
            "035420",
            market="kr",
            new_price=250000.0,
            dry_run=False,
            is_mock=False,
        )
    assert out["success"] is True
    mock_repoint.assert_awaited_once()
    _, kwargs = mock_repoint.await_args
    assert kwargs["old_order_no"] == "OLD"
    assert kwargs["new_order_no"] == "NEW"
