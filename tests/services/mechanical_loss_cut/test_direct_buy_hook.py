"""The direct order path writes a plan after an accepted live buy (ROB-1460)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_execution as oe
from app.services.mechanical_loss_cut import plans as plans_module

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _place(
    *,
    side: str,
    result: dict,
    symbol: str = "KRW-DOT",
    market: str = "crypto",
    **kwargs,
):
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
                    "symbol": symbol,
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
            symbol=symbol,
            side=side,
            market=market,
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


@pytest.mark.parametrize(
    ("symbol", "market", "expected_market"),
    [
        ("035720", "kr", "equity_kr"),
        ("035720", "equity_kr", "equity_kr"),
        ("035720", None, "equity_kr"),
        ("AAPL", "us", "equity_us"),
        ("AAPL", "equity_us", "equity_us"),
        ("AAPL", None, "equity_us"),
    ],
)
async def test_a_direct_kis_buy_reaches_the_plan_writer_as_plannable(
    monkeypatch, symbol, market, expected_market
):
    """The short code is resolved first, so a real plan is written, not a refusal."""
    monkeypatch.setattr(oe.settings, "order_approval_hash_mode", "off", raising=False)
    monkeypatch.setattr(settings, "LOT_EXIT_PLAN_WRITE_ENABLED", True)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    class Store:
        def __init__(self, _session):
            pass

        async def lock_lot(self, _key):
            return None

        async def latest_plan(self, _key):
            return None

        async def append_plan(self, draft):
            return draft

    async def flat(_session, _key):
        return None

    monkeypatch.setattr(plans_module, "MechanicalLossCutStore", Store)
    monkeypatch.setattr(plans_module, "load_position", flat)

    _response, recorder = await _place(
        side="buy", result={"success": True}, symbol=symbol, market=market
    )

    kwargs = recorder.await_args.kwargs
    assert kwargs["account_mode"] == "kis_live"
    assert kwargs["market"] == expected_market
    status = await plans_module.record_buy_exit_plan(**kwargs, session_factory=Session)
    assert status == "entry"
