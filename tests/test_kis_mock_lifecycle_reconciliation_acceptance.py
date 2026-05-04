"""Acceptance tests for KIS mock lifecycle reconciliation (ROB-102)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_full_reconciliation_cycle_acceptance(monkeypatch):
    """Verifies that place_order (kis_mock) -> reconciler detect fill -> MCP tool run."""
    from app.mcp_server.tooling import kis_mock_ledger, order_execution
    
    # 1. Setup mocks
    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )
    
    # Mock broker execution
    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(return_value={"rt_cd": "0", "odno": "ACC-1", "ord_tmd": "090000"}),
    )
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=100.0)
    )
    monkeypatch.setattr(
        order_execution, "_check_balance_and_warn", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(
        order_execution, "_check_daily_order_limit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())

    # Mock DB insert to return a real-looking ID
    save_ledger_mock = AsyncMock(return_value=555)
    monkeypatch.setattr(kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger_mock)

    # Mock Job's dependencies to avoid real DB/Broker
    mock_ledger_row = AsyncMock()
    mock_ledger_row.id = 555
    mock_ledger_row.symbol = "005930"
    mock_ledger_row.side = "buy"
    mock_ledger_row.quantity = 10
    mock_ledger_row.lifecycle_state = "accepted"
    mock_ledger_row.holdings_baseline_qty = Decimal("0")
    mock_ledger_row.trade_date = AsyncMock()

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [mock_ledger_row]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {
        "applied": True, "next_state": "fill"
    }
    
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )
    
    mock_kis = AsyncMock()
    mock_kis.fetch_my_stocks.return_value = [{"symbol": "005930", "qty": "10"}]
    monkeypatch.setattr("app.jobs.kis_mock_reconciliation_job.kis", mock_kis)

    # 2. Execution
    tools = build_tools()
    
    # Step A: Place order
    place_res = await tools["place_order"](
        symbol="005930",
        side="buy",
        quantity=10,
        price=100.0,
        account_mode="kis_mock",
        dry_run=False
    )
    assert place_res["success"] is True
    assert place_res["ledger_id"] == 555
    # Verify Task 3: lifecycle_state was passed as 'accepted'
    save_ledger_mock.assert_awaited_once()
    assert save_ledger_mock.call_args.kwargs["lifecycle_state"] == "accepted"

    # Step B: Run reconciliation tool
    recon_res = await tools["kis_mock_reconciliation_run"](dry_run=False)
    assert recon_res["orders_processed"] == 1
    assert recon_res["transitions_applied"] == 1
    
    # 3. Verify final state transition call
    mock_lifecycle_svc.apply_lifecycle_transition.assert_awaited_once()
    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 555
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
