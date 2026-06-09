"""ROB-461 — kis_mock equity sells may book a loss (손절 / stop-loss practice).

KIS mock is a practice sandbox with no real money, so the avg*1.01 floor and the
below-current-price guard must NOT block a loss-sell there. The bypass is scoped to
`is_mock AND market_type in {equity_kr, equity_us}` (the codebase idiom for "KIS mock
equities"); live (is_mock=False) and crypto (real Upbit funds) stay fully guarded.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling import order_validation
from app.mcp_server.tooling.order_validation import evaluate_sell_price_guards


# ---------------------------------------------------------------------------
# Pure guard: allow_loss_sell bypasses BOTH the avg*1.01 floor and current-price
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_allow_loss_sell_bypasses_floor_and_current_price() -> None:
    # price below floor (avg*1.01) AND below current price.
    err = evaluate_sell_price_guards(
        price=68000.0,
        current_price=68500.0,
        avg_price=90400.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_allow_loss_sell_bypasses_below_current_only() -> None:
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1100.0,
        avg_price=900.0,  # above floor, but below current
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_allow_loss_sell_false_still_blocks_floor() -> None:
    # Default (live) behavior is byte-for-byte unchanged.
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
        allow_loss_sell=False,
    )
    assert err is not None and "below minimum" in err


@pytest.mark.unit
def test_allow_loss_sell_default_is_false() -> None:
    # Omitting the kwarg keeps the floor guard (no accidental relaxation).
    err = evaluate_sell_price_guards(
        price=1000.0,
        current_price=1000.0,
        avg_price=1000.0,
        defensive_trim_ctx=None,
        scalping_exit_ctx=None,
    )
    assert err is not None and "below minimum" in err


# ---------------------------------------------------------------------------
# _preview_sell: mock equity allows loss; live + crypto stay blocked
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_preview_sell_mock_equity_allows_loss_sell(
    monkeypatch, market_type
) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    # The exact 2026-06-09 repro: DL이앤씨 avg 90,400 / current ~68,500, sell at 68,000.
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="limit",
        quantity=10,
        price=68000.0,
        current_price=68500.0,
        market_type=market_type,
        is_mock=True,
    )
    assert "error" not in result
    assert result["price"] == 68000.0
    assert result["realized_pnl"] < 0  # an honest loss is booked


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_equity_still_blocks_loss(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="limit",
        quantity=10,
        price=68000.0,
        current_price=68500.0,
        market_type="equity_kr",
        is_mock=False,
    )
    assert "error" in result and "below minimum" in result["error"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_mock_crypto_still_blocks_loss(monkeypatch) -> None:
    # Crypto routes to real Upbit funds; a kis_mock+crypto symbol must NOT relax
    # the floor even though is_mock=True (defense-in-depth).
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 40000000.0, "quantity": 0.1}),
    )
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="limit",
        quantity=0.1,
        price=30000000.0,
        current_price=31000000.0,
        market_type="crypto",
        is_mock=True,
    )
    assert "error" in result and "below minimum" in result["error"]


# ---------------------------------------------------------------------------
# _validate_sell_side: same matrix on the execution path
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_mock_equity_allows_loss_sell(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 90400.0, "quantity": 10}),
    )
    monkeypatch.setattr(
        order_validation,
        "_get_kis_mock_shadow_exposure",
        AsyncMock(
            return_value={
                "confidence": "db_shadow_pending",
                "sell_reserved_quantity": 0,
            }
        ),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="equity_kr",
        quantity=10,
        order_type="limit",
        price=68000.0,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=True,
        dry_run=True,
    )
    assert err is None and errors == []
    assert avg == 90400.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_mock_crypto_still_blocks_loss(monkeypatch) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": 40000000.0, "quantity": 0.1, "locked": 0}),
    )
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="KRW-BTC",
        normalized_symbol="KRW-BTC",
        market_type="crypto",
        quantity=0.1,
        order_type="limit",
        price=30000000.0,
        current_price=31000000.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=True,
        dry_run=True,
    )
    assert err is not None
    assert "below minimum" in errors[0]
