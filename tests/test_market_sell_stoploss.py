"""Tests for market sell stop-loss blocking logic (ROB-76).

Ensures both _preview_sell and _validate_sell_side block market sell orders
when current_price < avg_buy_price * 1.01.
"""

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation


@pytest.fixture(autouse=True)
def _patch_holdings(monkeypatch):
    """Provide default holdings for all tests."""
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(
            return_value={
                "quantity": 10.0,
                "avg_price": 100_000.0,
                "locked": 0.0,
            }
        ),
    )


# ── _preview_sell ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_market_sell_blocked_below_stoploss():
    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="market",
        quantity=None,
        price=None,
        current_price=100_500.0,
        market_type="kr",
    )
    assert "error" in result
    assert "Market sell blocked" in result["error"]
    assert "100500" in result["error"]


@pytest.mark.asyncio
async def test_preview_market_sell_allowed_above_stoploss():
    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="market",
        quantity=None,
        price=None,
        current_price=101_000.0,
        market_type="kr",
    )
    assert "error" not in result
    assert result["price"] == 101_000.0


@pytest.mark.asyncio
async def test_preview_market_sell_blocked_at_exact_avg():
    result = await order_validation._preview_sell(
        symbol="005930",
        order_type="market",
        quantity=None,
        price=None,
        current_price=100_000.0,
        market_type="kr",
    )
    assert "error" in result
    assert "Market sell blocked" in result["error"]


# ── _validate_sell_side ───────────────────────────────────────────────


def _make_error(msg: str) -> dict:
    return {"error": msg}


@pytest.mark.asyncio
async def test_validate_sell_side_market_blocked_below_stoploss():
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="kr",
        quantity=None,
        order_type="market",
        price=None,
        current_price=100_500.0,
        order_error_fn=_make_error,
    )
    assert err is not None
    assert "Market sell blocked" in err["error"]
    assert qty == 0.0


@pytest.mark.asyncio
async def test_validate_sell_side_market_allowed_above_stoploss():
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="kr",
        quantity=None,
        order_type="market",
        price=None,
        current_price=101_000.0,
        order_error_fn=_make_error,
    )
    assert err is None
    assert qty == 10.0
    assert avg == 100_000.0


@pytest.mark.asyncio
async def test_validate_sell_side_limit_still_blocked_below_stoploss():
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="005930",
        normalized_symbol="005930",
        market_type="kr",
        quantity=None,
        order_type="limit",
        price=100_500.0,
        current_price=101_000.0,
        order_error_fn=_make_error,
    )
    assert err is not None
    assert "below minimum" in err["error"]
