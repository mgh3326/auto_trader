"""Unit tests for KIS mock scalping sell-guard separation (ROB-321 PR1)."""

from __future__ import annotations

import pytest

from app.core.config import settings


@pytest.mark.unit
def test_kis_mock_scalping_disabled_by_default() -> None:
    assert settings.kis_mock_scalping_enabled is False


from app.mcp_server.tooling.order_validation import (
    DefensiveTrimContext,
    ScalpingExitContext,
    evaluate_sell_price_guards,
)
import datetime


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
        price=1000.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_guard_blocks_below_current_price_when_no_context() -> None:
    # price >= floor (avg low) but below current -> current-price error
    err = evaluate_sell_price_guards(
        price=1000.0, current_price=1100.0, avg_price=900.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_defensive_trim_bypasses_floor_but_not_current_price() -> None:
    # below floor: allowed by trim; but also below current -> still blocked
    err = evaluate_sell_price_guards(
        price=950.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=_trim_ctx(), scalping_exit_ctx=None,
    )
    assert err is not None and "below current price" in err


@pytest.mark.unit
def test_scalping_exit_bypasses_both_guards() -> None:
    # below floor AND below current: scalping exit allows it (stop-loss)
    err = evaluate_sell_price_guards(
        price=950.0, current_price=1000.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=_scalp_ctx(),
    )
    assert err is None


@pytest.mark.unit
def test_no_context_clean_price_passes() -> None:
    err = evaluate_sell_price_guards(
        price=1100.0, current_price=1050.0, avg_price=1000.0,
        defensive_trim_ctx=None, scalping_exit_ctx=None,
    )
    assert err is None


from app.mcp_server.tooling.order_validation import _resolve_scalping_exit_context


@pytest.mark.unit
def test_resolver_returns_none_when_not_requested(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=False, strategy_id="s", reason="stop_loss",
        side="sell", order_type="limit", is_mock=True,
    )
    assert ctx is None


@pytest.mark.unit
def test_resolver_fail_closed_on_live(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="kis_mock"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="limit", is_mock=False,
        )


@pytest.mark.unit
def test_resolver_fail_closed_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", False, raising=False)
    with pytest.raises(ValueError, match="KIS_MOCK_SCALPING_ENABLED"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="limit", is_mock=True,
        )


@pytest.mark.unit
def test_resolver_returns_context_when_authorized(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    ctx = _resolve_scalping_exit_context(
        scalping_exit=True, strategy_id="kis-mock-v1", reason="stop_loss",
        side="sell", order_type="limit", is_mock=True,
    )
    assert ctx is not None and ctx.strategy_id == "kis-mock-v1"
    assert ctx.reason == "stop_loss"


@pytest.mark.unit
def test_resolver_rejects_buy_and_market_and_bad_reason(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    with pytest.raises(ValueError, match="side='sell'"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="buy", order_type="limit", is_mock=True,
        )
    with pytest.raises(ValueError, match="order_type='limit'"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="stop_loss",
            side="sell", order_type="market", is_mock=True,
        )
    with pytest.raises(ValueError, match="reason"):
        _resolve_scalping_exit_context(
            scalping_exit=True, strategy_id="s", reason="moon",
            side="sell", order_type="limit", is_mock=True,
        )


