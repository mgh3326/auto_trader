"""The direct order path writes a plan after an accepted live buy (ROB-1460)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import order_execution as oe

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _place(*, side: str, result: dict, **kwargs):
    recorder = AsyncMock(return_value="entry")
    executed = AsyncMock(return_value=result)
    with (
        patch.object(
            oe, "_check_balance_and_warn", new=AsyncMock(return_value=(None, None))
        ),
        patch.object(oe, "_fetch_current_price", new=AsyncMock(return_value=1250.0)),
        patch.object(
            oe,
            "_build_preview",
            new=AsyncMock(
                return_value={
                    "symbol": "KRW-DOT",
                    "side": side,
                    "order_type": "limit",
                    "price": 1244.0,
                    "quantity": 10.0,
                    "estimated_value": 12440.0,
                }
            ),
        ),
        patch.object(
            oe,
            "_validate_sell_side",
            new=AsyncMock(return_value=(10.0, 1000.0, None)),
        ),
        patch.object(oe, "_execute_and_record", new=executed),
        patch.object(oe, "record_buy_exit_plan", new=recorder),
    ):
        response = await oe._place_order_impl(
            symbol="KRW-DOT",
            side=side,
            market="crypto",
            order_type="limit",
            price=1244.0,
            quantity=10,
            dry_run=False,
            thesis="test thesis",
            strategy="test",
            **kwargs,
        )
    # Without this, a test that expects no plan could pass on an earlier error.
    executed.assert_awaited_once()
    return response, recorder


async def test_accepted_direct_buy_records_a_plan(monkeypatch):
    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)

    response, recorder = await _place(side="buy", result={"success": True})

    assert response == {"success": True}
    recorder.assert_awaited_once()
    assert recorder.await_args.kwargs["account_mode"] == "upbit"
    assert recorder.await_args.kwargs["market"] == "crypto"
    assert recorder.await_args.kwargs["price"] == 1244.0
    assert recorder.await_args.kwargs["quantity"] == 10.0


async def test_rejected_direct_buy_records_nothing(monkeypatch):
    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)

    _response, recorder = await _place(side="buy", result={"success": False})

    recorder.assert_not_awaited()


async def test_proposal_flow_leaves_the_plan_to_revalidation(monkeypatch):
    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)

    _response, recorder = await _place(
        side="buy", result={"success": True}, proposal_flow=True
    )

    recorder.assert_not_awaited()


async def test_a_sell_records_nothing(monkeypatch):
    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)

    _response, recorder = await _place(side="sell", result={"success": True})

    recorder.assert_not_awaited()
