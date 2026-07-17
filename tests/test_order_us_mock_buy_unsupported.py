"""ROB-417 — US kis_mock buy is fail-closed unsupported (OPSQ0002), made explicit."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _get_balance_for_order,
    _kis_mock_us_orderable_unsupported,
)
from app.services.us_dual_paper.capability_matrix import get_capability_matrix


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


def test_kis_mock_us_orderable_unsupported_reflects_capability_matrix():
    # ROB-951: VTTS3007R provides verified USD buying power in mock mode.
    assert _kis_mock_us_orderable_unsupported() is False


def test_capability_matrix_changes_only_kis_mock_cash_read():
    matrix = get_capability_matrix()
    assert matrix["kis_mock"]["account_cash_read"] is True
    assert matrix["kis_mock"]["open_orders_read"] is False
    assert matrix["alpaca_paper"]["account_cash_read"] is True
    assert matrix["alpaca_paper"]["open_orders_read"] is True


@pytest.mark.asyncio
async def test_us_mock_buy_uses_verified_vtts3007_orderable_cash(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 99_996.18

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert warning is None
    assert error is None
    assert warning is None
    assert called["balance"] is True


@pytest.mark.asyncio
async def test_us_mock_buy_vtts3007_failure_remains_fail_closed(monkeypatch):
    async def spy_balance(*_a, **_k):
        raise RuntimeError("VTTS3007R timeout")

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert warning is None
    assert error is not None
    assert "VTTS3007R timeout" in error["error"]
    assert "refusing to submit without verified orderable cash" in error["error"]


@pytest.mark.asyncio
async def test_us_mock_buy_blocks_when_vtts3007_orderable_is_insufficient(monkeypatch):
    async def spy_balance(*_a, **_k):
        return 99.99

    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)
    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=100.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )

    assert warning is None
    assert error is not None
    assert "Insufficient USD balance" in error["error"]


@pytest.mark.asyncio
async def test_kr_mock_buy_not_guarded_enters_balance_path(monkeypatch):
    called = {"balance": False}

    async def spy_balance(market_type, is_mock=False):
        called["balance"] = True
        return 10_000_000.0  # ample KRW

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    # KR mock has a DB-shadow-exposure guard; stub it to the pass-through state.
    async def fake_exposure(*_a, **_k):
        return {"confidence": "db_shadow_pending", "buy_reserved_amount": 0.0}

    monkeypatch.setattr(
        order_validation, "_get_kis_mock_shadow_exposure", fake_exposure
    )

    warning, error = await _check_balance_and_warn(
        market_type="equity_kr",
        normalized_symbol="005930",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert error is None
    assert called["balance"] is True  # guard did NOT short-circuit KR


@pytest.mark.asyncio
async def test_us_live_buy_not_guarded(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 10_000.0

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=False,
        order_error_fn=_order_error,
        is_mock=False,  # live
    )
    assert error is None
    assert called["balance"] is True  # live enters the real precheck


@pytest.mark.asyncio
async def test_us_live_balance_keeps_live_orderable_helper(monkeypatch):
    async def live_orderable(account_token: str) -> float:
        assert account_token == "kis_overseas"
        return 321.0

    monkeypatch.setattr(order_validation, "_live_kis_orderable", live_orderable)
    monkeypatch.setattr(
        order_validation,
        "_create_kis_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("mock client used")),
    )

    assert await _get_balance_for_order("equity_us", is_mock=False) == 321.0
