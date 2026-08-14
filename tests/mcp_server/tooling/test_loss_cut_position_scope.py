from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import LossCutContext


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_domestic_zero_orderable_does_not_fall_back_to_total(monkeypatch):
    client = type(
        "Client",
        (),
        {
            "fetch_my_stocks": AsyncMock(
                return_value=[
                    {
                        "pdno": "005930",
                        "hldg_qty": "8",
                        "ord_psbl_qty": "0",
                        "pchs_avg_pric": "70000",
                    }
                ]
            )
        },
    )()
    monkeypatch.setattr(
        order_validation, "_create_kis_client", lambda *, is_mock: client
    )

    result = await order_validation._get_holdings_for_order(
        "005930", "equity_kr", is_mock=False
    )

    assert result == {
        "quantity": 0.0,
        "total_quantity": 8.0,
        "locked": 8.0,
        "avg_price": 70000.0,
        "sellable_observed": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kis_overseas_uses_common_orderable_field(monkeypatch):
    client = type(
        "Client",
        (),
        {
            "fetch_my_us_stocks": AsyncMock(
                return_value=[
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_cblc_qty": "10",
                        "ord_psbl_qty": "7",
                        "pchs_avg_pric": "180",
                    }
                ]
            )
        },
    )()
    monkeypatch.setattr(
        order_validation, "_create_kis_client", lambda *, is_mock: client
    )

    result = await order_validation._get_holdings_for_order(
        "AAPL", "equity_us", is_mock=False
    )

    assert result is not None
    assert result["quantity"] == 7.0
    assert result["total_quantity"] == 10.0
    assert result["locked"] == 3.0
    assert result["sellable_observed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loss_cut_preview_requires_fresh_orderable_quantity(monkeypatch):
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "quantity": 8.0,
                "total_quantity": 8.0,
                "locked": 0.0,
                "avg_price": 200.0,
                "sellable_observed": False,
            }
        ),
    )
    context = LossCutContext(
        retrospective_id=42,
        exit_reason="stop_loss",
        approval_issue_id=None,
        requester_agent_id="fixture-agent",
        max_slip=0.02,
        approval_verified_at=datetime.now(UTC),
    )

    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="limit",
        quantity=1.0,
        price=99.0,
        current_price=100.0,
        market_type="equity_kr",
        loss_cut_ctx=context,
    )

    assert result["error"] == (
        "Fresh orderable quantity is required for a loss_cut preview."
    )
