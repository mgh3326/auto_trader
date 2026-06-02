"""ROB-417 — US kis_mock buy is fail-closed unsupported (OPSQ0002), made explicit."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    _check_balance_and_warn,
    _kis_mock_us_orderable_unsupported,
)


def _order_error(message: str) -> dict:
    return {"success": False, "error": message}


def test_kis_mock_us_orderable_unsupported_reflects_capability_matrix():
    # capability_matrix documents kis_mock account_cash_read=False (OPSQ0002).
    assert _kis_mock_us_orderable_unsupported() is True


@pytest.mark.asyncio
async def test_us_mock_buy_non_dry_run_blocked_with_mock_unsupported(monkeypatch):
    called = {"balance": False}

    async def spy_balance(*_a, **_k):
        called["balance"] = True
        return 0.0

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
    assert error["success"] is False
    assert error["mock_unsupported"] is True
    assert error["capability"] == "kis_mock_us_orderable_cash_unsupported"
    assert "unsupported" in error["error"].lower()
    # Early guard short-circuits BEFORE any KIS network call.
    assert called["balance"] is False


@pytest.mark.asyncio
async def test_us_mock_buy_dry_run_returns_clear_warning_keeps_preview(monkeypatch):
    async def spy_balance(*_a, **_k):
        raise AssertionError("must not be called for US mock buy guard")

    monkeypatch.setattr(order_validation, "_get_balance_for_order", spy_balance)

    warning, error = await _check_balance_and_warn(
        market_type="equity_us",
        normalized_symbol="MSFT",
        side="buy",
        order_amount=1000.0,
        dry_run=True,
        order_error_fn=_order_error,
        is_mock=True,
    )
    assert error is None  # preview not blocked
    assert warning is not None
    assert "US mock buy unsupported" in warning


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
