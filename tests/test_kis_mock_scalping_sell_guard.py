"""Unit tests for KIS mock scalping sell-guard separation (ROB-321 PR1)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    ScalpingExitContext,
    _resolve_scalping_exit_context,
    evaluate_sell_price_guards,
)


@pytest.mark.unit
def test_kis_mock_scalping_disabled_by_default() -> None:
    assert settings.kis_mock_scalping_enabled is False


def _trim_ctx() -> DefensiveTrimContext:
    return DefensiveTrimContext(
        approval_issue_id="ROB-1",
        requester_agent_id="agent",
        approval_verified_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )


def _scalp_ctx() -> ScalpingExitContext:
    return ScalpingExitContext(strategy_id="kis-mock-v1", reason="stop_loss")


@pytest.mark.unit
def test_guard_blocks_below_floor_when_no_context() -> None:
    # price below avg*1.01 -> floor error
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_guard_blocks_below_current_price_when_no_context() -> None:
    # price >= floor (avg low) but below current -> current-price error
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1100.0,
        avg_price=900.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )
    assert err is not None and "below marketable band floor" in err


@pytest.mark.unit
def test_defensive_trim_bypasses_floor_but_not_current_price() -> None:
    # below floor: allowed by trim; but also below current -> still blocked
    err = evaluate_sell_price_guards(
        price=950.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=_trim_ctx(),
        scalping_exit_ctx=None,
    )
    assert err is not None and "below marketable band floor" in err


@pytest.mark.unit
def test_scalping_exit_bypasses_both_guards() -> None:
    # below floor AND below current: scalping exit allows it (stop-loss)
    err = evaluate_sell_price_guards(
        price=950.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=_scalp_ctx(),
    )
    assert err is None


@pytest.mark.unit
def test_no_context_clean_price_passes() -> None:
    err = evaluate_sell_price_guards(
        price=1100.0,
        current_price=1050.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )
    assert err is None


@pytest.mark.unit
def test_resolver_returns_none_when_not_requested(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=False,
        strategy_id="s",
        reason="stop_loss",
        side="sell",
        order_type="limit",
        is_mock=True,
    )
    assert ctx is None


@pytest.mark.unit
def test_resolver_fail_closed_on_live(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="kis_mock"):
        _resolve_scalping_exit_context(
            scalping_exit=True,
            strategy_id="s",
            reason="stop_loss",
            side="sell",
            order_type="limit",
            is_mock=False,
        )


@pytest.mark.unit
def test_resolver_fail_closed_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", False, raising=False)
    with pytest.raises(ValueError, match="KIS_MOCK_SCALPING_ENABLED"):
        _resolve_scalping_exit_context(
            scalping_exit=True,
            strategy_id="s",
            reason="stop_loss",
            side="sell",
            order_type="limit",
            is_mock=True,
        )


@pytest.mark.unit
def test_resolver_returns_context_when_authorized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=True,
        strategy_id="kis-mock-v1",
        reason="stop_loss",
        side="sell",
        order_type="limit",
        is_mock=True,
    )
    assert ctx is not None and ctx.strategy_id == "kis-mock-v1"
    assert ctx.reason == "stop_loss"


@pytest.mark.unit
def test_resolver_rejects_buy_and_market_and_bad_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="side='sell'"):
        _resolve_scalping_exit_context(
            scalping_exit=True,
            strategy_id="s",
            reason="stop_loss",
            side="buy",
            order_type="limit",
            is_mock=True,
        )
    with pytest.raises(ValueError, match="order_type='limit'"):
        _resolve_scalping_exit_context(
            scalping_exit=True,
            strategy_id="s",
            reason="stop_loss",
            side="sell",
            order_type="market",
            is_mock=True,
        )
    with pytest.raises(ValueError, match="reason"):
        _resolve_scalping_exit_context(
            scalping_exit=True,
            strategy_id="s",
            reason="moon",
            side="sell",
            order_type="limit",
            is_mock=True,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_scalping_exit_allows_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="limit",
        quantity=10,
        price=950.0,
        current_price=980.0,
        market_type="kr",
        scalping_exit_ctx=ScalpingExitContext(strategy_id="s", reason="stop_loss"),
        is_mock=True,
    )
    assert "error" not in result
    assert result["price"] == 950.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_still_blocks_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    # No scalping ctx, no trim ctx: live behavior preserved.
    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="limit",
        quantity=10,
        price=950.0,
        current_price=980.0,
        market_type="kr",
        is_mock=False,
    )
    assert "error" in result and "below minimum" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_scalping_exit_allows_below_floor(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 1000.0, "quantity": 10}),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="kr",
        quantity=10,
        order_type="limit",
        price=950.0,
        current_price=980.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        scalping_exit_ctx=ScalpingExitContext(strategy_id="s", reason="stop_loss"),
        is_mock=True,
        dry_run=True,
    )
    assert err is None and errors == []
    assert avg == 1000.0
