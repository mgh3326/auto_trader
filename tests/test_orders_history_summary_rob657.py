from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.mcp_server.tooling.orders_history as orders_history
from app.mcp_server.tooling.orders_history import _calculate_order_summary


@pytest.mark.unit
def test_calculate_order_summary_counts_expired() -> None:
    orders = [
        {"status": "expired"},
        {"status": "pending"},
        {"status": "filled"},
        {"status": "expired"},
    ]

    summary = _calculate_order_summary(orders)

    assert summary["expired"] == 2
    assert summary["pending"] == 1
    assert summary["filled"] == 1
    assert summary["total_orders"] == 4


_KIA_DEAD_ROW = {
    "ord_dt": "20260702",
    "ord_tmd": "100800",
    "odno": "0013894000",
    "sll_buy_dvsn_cd": "02",
    "pdno": "000270",
    "prdt_name": "기아",
    "ord_qty": "8",
    "ord_unpr": "129600",
    "tot_ccld_qty": "0",
    "rmn_qty": "0",
}


def _patch_kr(rows: list[dict]):
    # inquire_daily_order_domestic is only hit for status in (filled/cancelled);
    # return [] there so the pending path is what matters.
    client = AsyncMock()
    client.inquire_korea_orders = AsyncMock(return_value=rows)
    client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    return patch.object(orders_history, "_create_kis_client", lambda *, is_mock: client)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_history_all_marks_dead_order_expired() -> None:
    with _patch_kr([_KIA_DEAD_ROW]):
        resp = await orders_history.get_order_history_impl(
            symbol="000270", status="all", market="kr", is_mock=False
        )

    orders = resp["orders"]
    assert len(orders) == 1
    assert orders[0]["order_id"] == "0013894000"
    assert orders[0]["status"] == "expired"
    assert orders[0]["is_live"] is False
    assert resp["summary"]["expired"] == 1
    assert resp["summary"]["pending"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_history_pending_excludes_dead_order() -> None:
    with _patch_kr([_KIA_DEAD_ROW]):
        resp = await orders_history.get_order_history_impl(
            symbol="000270", status="pending", market="kr", is_mock=False
        )

    assert resp["orders"] == []
    assert resp["summary"]["pending"] == 0
    assert resp["summary"]["expired"] == 0
