# tests/mcp_server/tooling/test_order_execution_live_routing.py
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_kr_routes_to_ledger_not_record_fill():
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(
            oe,
            "_execute_order",
            new=AsyncMock(return_value={"rt_cd": "0", "odno": "X1"}),
        ),
        patch.object(oe, "_record_order_history", new=AsyncMock(return_value=None)),
        patch.object(oe, "_check_daily_order_limit", new=AsyncMock(return_value=True)),
        patch(
            "app.mcp_server.tooling.kis_live_ledger._record_kis_live_order",
            new=AsyncMock(
                return_value={"broker_status": "accepted", "fill_recorded": False}
            ),
        ) as mock_ledger,
        patch.object(oe, "_record_fill_and_journals", new=AsyncMock()) as mock_record,
    ):
        out = await oe._execute_and_record(
            normalized_symbol="035420",
            side="sell",
            order_type="limit",
            order_quantity=10,
            price=250000,
            market_type="equity_kr",
            current_price=250000,
            avg_price=0.0,
            dry_run_result={
                "price": 250000,
                "quantity": 10,
                "estimated_value": 2500000,
            },
            order_amount=2500000,
            reason="r",
            exit_reason="take_profit",
            thesis=None,
            strategy=None,
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
            defensive_trim_ctx=None,
            order_error_fn=lambda m: {"success": False, "error": m},
            is_mock=False,
        )
    mock_ledger.assert_awaited_once()
    mock_record.assert_not_awaited()  # live KR must NOT pre-book fill/journal
    assert out["fill_recorded"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_us_still_uses_record_fill():
    """US live is out of scope — keeps the legacy path unchanged."""
    from app.mcp_server.tooling import order_execution as oe

    with (
        patch.object(
            oe,
            "_execute_order",
            new=AsyncMock(return_value={"rt_cd": "0", "odno": "U1"}),
        ),
        patch.object(oe, "_record_order_history", new=AsyncMock(return_value=None)),
        patch.object(oe, "_check_daily_order_limit", new=AsyncMock(return_value=True)),
        patch.object(
            oe,
            "_record_fill_and_journals",
            new=AsyncMock(return_value={"fill_recorded": True}),
        ) as mock_record,
    ):
        await oe._execute_and_record(
            normalized_symbol="AAPL",
            side="buy",
            order_type="limit",
            order_quantity=1,
            price=100,
            market_type="equity_us",
            current_price=100,
            avg_price=0.0,
            dry_run_result={"price": 100, "quantity": 1, "estimated_value": 100},
            order_amount=100,
            reason="r",
            exit_reason=None,
            thesis="t",
            strategy="s",
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes=None,
            indicators_snapshot=None,
            defensive_trim_ctx=None,
            order_error_fn=lambda m: {"success": False, "error": m},
            is_mock=False,
        )
    mock_record.assert_awaited_once()
