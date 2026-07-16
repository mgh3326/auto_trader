"""ROB-321 PR4 — ScalpingExitContext wiring through _place_order_impl.

PR1 added the context + resolver + guard bypass but left it unwired (forward
scaffolding). These tests pin that _place_order_impl now resolves and threads it:
fail-closed on live / flag-off, and bypasses the sell floor for a mock scalping
exit. (The executor in PR4 calls _place_order_impl with scalping_exit=True.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.mcp_server.tooling import order_execution, order_validation


@pytest.fixture(autouse=True)
def _mock_io(monkeypatch: pytest.MonkeyPatch):
    # avg 70000 -> floor 70700; current 69500. A scalping stop-loss at 69000 is
    # below BOTH the avg*1.01 floor and the current price.
    monkeypatch.setattr(
        order_execution, "_fetch_current_price", AsyncMock(return_value=69500.0)
    )
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 70000.0, "quantity": 10}),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_kis_mock_shadow_exposure",
        AsyncMock(return_value={"sell_reserved_quantity": 0}),
    )


async def _place(**kw):
    params = {
        "symbol": "005930",
        "side": "sell",
        "market": "kr",
        "order_type": "limit",
        "quantity": 1,
        "price": 69000.0,
        "dry_run": True,
    }
    params.update(kw)
    return await order_execution._place_order_impl(**params)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalping_exit_fail_closed_on_live(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    result = await _place(
        is_mock=False, scalping_exit=True, scalping_strategy_id="kis-mock-v1"
    )
    assert result["success"] is False
    assert "kis_mock" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scalping_exit_fail_closed_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", False, raising=False)
    result = await _place(
        is_mock=True, scalping_exit=True, scalping_strategy_id="kis-mock-v1"
    )
    assert result["success"] is False
    assert "KIS_MOCK_SCALPING_ENABLED" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mock_scalping_exit_bypasses_sell_floor(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    result = await _place(
        is_mock=True, scalping_exit=True, scalping_strategy_id="kis-mock-v1"
    )
    # The dry-run preview must NOT be rejected by the avg*1.01 floor or the
    # current-price guard — the scalping exit context bypasses both.
    err = result.get("error", "")
    assert "below minimum" not in err
    assert "below current price" not in err
    assert "below marketable band floor" not in err


@pytest.mark.unit
@pytest.mark.asyncio
async def test_live_sell_below_floor_still_blocked(monkeypatch) -> None:
    # Regression: no scalping_exit, live path -> floor still enforced.
    monkeypatch.setattr(settings, "kis_mock_scalping_enabled", True, raising=False)
    result = await _place(is_mock=False)
    assert result["success"] is False
    assert (
        "below minimum" in result["error"]
        or "below marketable band floor" in result["error"]
    )
