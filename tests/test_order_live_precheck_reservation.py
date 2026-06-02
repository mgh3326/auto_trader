"""ROB-419 — live buy precheck uses reservation-aware orderable (== get_available_capital)."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _get_balance_for_order,
)


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


@pytest.mark.asyncio
async def test_live_kr_precheck_uses_reservation_adjusted_orderable(monkeypatch):
    seen = {}

    async def fake_cash(account=None, *, is_mock=False):
        seen["account"] = account
        seen["is_mock"] = is_mock
        # raw orderable was higher, but pending orders reserved it to 0.
        return {
            "accounts": [
                {"account": "kis_domestic", "currency": "KRW", "orderable": 0.0}
            ]
        }

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    balance = await _get_balance_for_order("equity_kr", is_mock=False)
    assert balance == 0.0
    assert seen == {"account": "kis_domestic", "is_mock": False}


@pytest.mark.asyncio
async def test_live_us_precheck_uses_reservation_adjusted_orderable(monkeypatch):
    seen = {}

    async def fake_cash(account=None, *, is_mock=False):
        seen["account"] = account
        return {
            "accounts": [
                {"account": "kis_overseas", "currency": "USD", "orderable": 0.0}
            ]
        }

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    balance = await _get_balance_for_order("equity_us", is_mock=False)
    assert balance == 0.0
    assert seen["account"] == "kis_overseas"


@pytest.mark.asyncio
async def test_live_us_buy_blocked_when_orderable_reserved_to_zero(monkeypatch):
    # repro: pending orders reserved all cash → orderable=0 → buy must NOT pass.
    async def fake_cash(account=None, *, is_mock=False):
        return {
            "accounts": [
                {"account": "kis_overseas", "currency": "USD", "orderable": 0.0}
            ]
        }

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    # dry_run: insufficient warning (no error, preview still returned upstream).
    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=False,
    )
    assert error is None
    assert warning is not None and "Insufficient" in warning

    # non-dry_run: hard error.
    warning2, error2 = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,
    )
    assert error2 is not None
    assert error2["success"] is False


@pytest.mark.asyncio
async def test_mock_kr_precheck_does_not_delegate_to_cash_balance(monkeypatch):
    called = {"cash": False}

    async def fake_cash(account=None, *, is_mock=False):
        called["cash"] = True
        return {"accounts": []}

    monkeypatch.setattr(order_validation, "get_cash_balance_impl", fake_cash)

    class FakeKIS:
        def __init__(self, is_mock: bool = False):
            self.is_mock = is_mock

        async def inquire_domestic_cash_balance(self, *, is_mock: bool = False):
            return {"stck_cash_ord_psbl_amt": 5_000_000}

    monkeypatch.setattr(order_validation, "KISClient", FakeKIS)

    balance = await _get_balance_for_order("equity_kr", is_mock=True)
    assert balance == 5_000_000.0
    assert called["cash"] is False  # mock path must NOT use get_cash_balance_impl
