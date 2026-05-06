"""Acceptance tests for KIS mock lifecycle reconciliation (ROB-102)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests._mcp_tooling_support import build_tools


@pytest.mark.asyncio
async def test_full_reconciliation_cycle_acceptance(monkeypatch):
    """place_order (kis_mock) captures baseline → reconciler detects fill via MCP tool."""
    from app.mcp_server.tooling import kis_mock_ledger, order_execution

    monkeypatch.setattr(
        "app.mcp_server.tooling.orders_registration.validate_kis_mock_config",
        lambda: [],
    )

    # Broker / pre-execute path stubs.
    monkeypatch.setattr(
        order_execution,
        "_execute_order",
        AsyncMock(return_value={"rt_cd": "0", "odno": "ACC-1", "ord_tmd": "090000"}),
    )
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=100.0)
    )
    monkeypatch.setattr(
        order_execution,
        "_check_balance_and_warn",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr(
        order_execution, "_check_daily_order_limit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(order_execution, "_record_order_history", AsyncMock())

    # ROB-102: pre-order baseline lookup → simulate "no prior position".
    monkeypatch.setattr(
        kis_mock_ledger,
        "_fetch_kis_mock_baseline_qty",
        AsyncMock(return_value=Decimal("0")),
    )

    # Capture the ledger insert and assert lifecycle_state + baseline propagate.
    save_ledger_mock = AsyncMock(return_value=555)
    monkeypatch.setattr(
        kis_mock_ledger, "_save_kis_mock_order_ledger", save_ledger_mock
    )

    # Reconciliation: stub the lifecycle service + KIS client (mock holdings).
    mock_ledger_row = MagicMock()
    mock_ledger_row.id = 555
    mock_ledger_row.symbol = "005930"
    mock_ledger_row.side = "buy"
    mock_ledger_row.quantity = Decimal("10")
    mock_ledger_row.lifecycle_state = "accepted"
    mock_ledger_row.holdings_baseline_qty = Decimal("0")
    mock_ledger_row.trade_date = datetime.now(UTC) - timedelta(seconds=10)

    mock_lifecycle_svc = AsyncMock()
    mock_lifecycle_svc.list_open_orders.return_value = [mock_ledger_row]
    mock_lifecycle_svc.apply_lifecycle_transition.return_value = {
        "applied": True,
        "next_state": "fill",
    }
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISMockLifecycleService",
        lambda _: mock_lifecycle_svc,
    )

    fake_kis = MagicMock()
    fake_kis.fetch_my_stocks = AsyncMock(
        side_effect=[
            [{"pdno": "005930", "hldg_qty": "10"}],  # KR mock holdings post-fill
            [],  # US mock holdings
        ]
    )
    monkeypatch.setattr(
        "app.jobs.kis_mock_reconciliation_job.KISClient",
        lambda *a, **kw: fake_kis,
    )

    # Use AsyncSessionLocal from production path; reconciler tool only opens a
    # session and passes it to the job, which we've fully mocked above.
    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    fake_session_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(
        kis_mock_ledger,
        "_order_session_factory",
        lambda: lambda: fake_session_cm,
    )

    tools = build_tools()

    # Step A: place_order writes ledger row with baseline + lifecycle_state.
    place_res = await tools["place_order"](
        symbol="005930",
        side="buy",
        quantity=10,
        price=100.0,
        account_mode="kis_mock",
        dry_run=False,
    )
    assert place_res["success"] is True
    assert place_res["ledger_id"] == 555
    save_ledger_mock.assert_awaited_once()
    save_kwargs = save_ledger_mock.call_args.kwargs
    assert save_kwargs["lifecycle_state"] == "accepted"
    assert save_kwargs["holdings_baseline_qty"] == pytest.approx(Decimal("0"))

    # Step B: dry_run=False without confirm is rejected.
    rejected = await tools["kis_mock_reconciliation_run"](dry_run=False, confirm=False)
    assert rejected["success"] is False
    assert "confirm" in rejected["error"].lower()

    # Step C: dry_run=False, confirm=True applies the fill transition.
    recon_res = await tools["kis_mock_reconciliation_run"](dry_run=False, confirm=True)
    assert recon_res["orders_processed"] == 1
    assert recon_res["transitions_applied"] == 1
    assert recon_res["account_mode"] == "kis_mock"

    args = mock_lifecycle_svc.apply_lifecycle_transition.call_args.kwargs
    assert args["ledger_id"] == 555
    assert args["next_state"] == "fill"
    assert args["reason_code"] == "fill_detected"
    assert args["dry_run"] is False
