"""ROB-518 — live accounts must not realize a loss-sell by mistake.

Live limit sells were already double-guarded (avg*1.01 floor + below-current
block), but two live paths bypassed the floor entirely:

1. market sells — ``_preview_sell`` / ``_validate_sell_side`` only ran the
   guards for ``order_type == "limit"``, so a live market sell with the
   current price below the avg*1.01 floor went straight through (reachable
   via POST /api/screener/order and any internal ``_place_order_impl`` caller).
2. modify — ``modify_order_impl`` re-priced a resting live sell order with no
   floor check, so a guarded placement could be repriced into a loss.

Mock equity keeps the ROB-461 ``allow_loss_sell`` bypass (손절 practice).
defensive_trim / scalping_exit are limit-only by precondition and unaffected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from app.mcp_server.tooling import order_validation, orders_modify_cancel
from app.mcp_server.tooling.order_validation import (
    evaluate_market_sell_loss_guard,
)


# ---------------------------------------------------------------------------
# Pure guard: evaluate_market_sell_loss_guard
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_market_guard_blocks_current_below_floor() -> None:
    err = evaluate_market_sell_loss_guard(
        current_price=68500.0,
        avg_price=90400.0,
        allow_loss_sell=False,
    )
    assert err is not None and "market sell blocked" in err.lower()


@pytest.mark.unit
def test_market_guard_allows_current_at_or_above_floor() -> None:
    assert (
        evaluate_market_sell_loss_guard(
            current_price=1010.0, avg_price=1000.0, allow_loss_sell=False
        )
        is None
    )
    assert (
        evaluate_market_sell_loss_guard(
            current_price=1200.0, avg_price=1000.0, allow_loss_sell=False
        )
        is None
    )


@pytest.mark.unit
def test_market_guard_mock_bypass_allows_loss() -> None:
    err = evaluate_market_sell_loss_guard(
        current_price=68500.0,
        avg_price=90400.0,
        allow_loss_sell=True,
    )
    assert err is None


@pytest.mark.unit
def test_market_guard_unknown_basis_fails_open() -> None:
    # avg_price <= 0 means the cost basis is unknown (e.g. manual holdings rows
    # without it); the limit guard has always been fail-open there — keep parity.
    assert (
        evaluate_market_sell_loss_guard(
            current_price=100.0, avg_price=0.0, allow_loss_sell=False
        )
        is None
    )


@pytest.mark.unit
def test_market_guard_default_is_blocking() -> None:
    # Omitting allow_loss_sell must keep the floor (no accidental relaxation).
    err = evaluate_market_sell_loss_guard(current_price=900.0, avg_price=1000.0)
    assert err is not None


# ---------------------------------------------------------------------------
# _preview_sell: live market sells below floor are blocked; mock equity stays open
# ---------------------------------------------------------------------------
def _patch_holdings(monkeypatch, avg_price: float, quantity: float = 10) -> None:
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"avg_price": avg_price, "quantity": quantity}),
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us", "crypto"])
async def test_preview_sell_live_market_loss_blocked(monkeypatch, market_type) -> None:
    _patch_holdings(monkeypatch, avg_price=90400.0)
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type=market_type,
        is_mock=False,
    )
    assert "error" in result and "market sell blocked" in result["error"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_live_market_profit_allowed(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=60000.0)
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type="equity_kr",
        is_mock=False,
    )
    assert "error" not in result
    assert result["price"] == 68500.0
    assert result["realized_pnl"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("market_type", ["equity_kr", "equity_us"])
async def test_preview_sell_mock_equity_market_loss_allowed(
    monkeypatch, market_type
) -> None:
    # ROB-461 practice semantics survive: mock equity may book a market loss.
    _patch_holdings(monkeypatch, avg_price=90400.0)
    log_mock = Mock()
    monkeypatch.setattr(order_validation, "_log_mock_loss_sell_bypass", log_mock)
    result = await order_validation._preview_sell(
        symbol="375500",
        order_type="market",
        quantity=10,
        price=None,
        current_price=68500.0,
        market_type=market_type,
        is_mock=True,
    )
    assert "error" not in result
    assert result["realized_pnl"] < 0
    # The bypass is audited like the limit-path one.
    assert log_mock.call_args.kwargs["phase"] == "preview"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_sell_mock_crypto_market_loss_still_blocked(monkeypatch) -> None:
    # Crypto routes to real Upbit funds; is_mock never relaxes it.
    _patch_holdings(monkeypatch, avg_price=40000000.0, quantity=0.1)
    result = await order_validation._preview_sell(
        symbol="KRW-BTC",
        order_type="market",
        quantity=0.1,
        price=None,
        current_price=31000000.0,
        market_type="crypto",
        is_mock=True,
    )
    assert "error" in result and "market sell blocked" in result["error"].lower()


# ---------------------------------------------------------------------------
# _validate_sell_side: execution path mirrors the preview matrix
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_live_market_loss_blocked(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=90400.0)
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="equity_kr",
        quantity=10,
        order_type="market",
        price=None,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=False,
        dry_run=False,
    )
    assert err is not None
    assert "market sell blocked" in errors[0].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_mock_equity_market_loss_allowed(monkeypatch) -> None:
    _patch_holdings(monkeypatch, avg_price=90400.0)
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
    log_mock = Mock()
    monkeypatch.setattr(order_validation, "_log_mock_loss_sell_bypass", log_mock)
    errors: list[str] = []
    qty, avg, err = await order_validation._validate_sell_side(
        symbol="375500",
        normalized_symbol="375500",
        market_type="equity_kr",
        quantity=10,
        order_type="market",
        price=None,
        current_price=68500.0,
        order_error_fn=lambda m: errors.append(m) or {"error": m},
        is_mock=True,
        dry_run=True,
    )
    assert err is None and errors == []
    assert log_mock.call_args.kwargs["phase"] == "execution"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validate_sell_side_live_limit_guard_unchanged(monkeypatch) -> None:
    # Regression: the existing live limit guard message is byte-for-byte intact.
    _patch_holdings(monkeypatch, avg_price=90400.0)
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
        is_mock=False,
        dry_run=False,
    )
    assert err is not None and "below minimum" in errors[0]


# ---------------------------------------------------------------------------
# modify_order: live sell orders must not be repriced below the floor
# ---------------------------------------------------------------------------
def _patch_modify_holdings(monkeypatch, avg_price: float | None) -> AsyncMock:
    holdings = {"avg_price": avg_price, "quantity": 10} if avg_price else {}
    mock = AsyncMock(return_value=holdings)
    monkeypatch.setattr(orders_modify_cancel, "_get_holdings_for_order", mock)
    return mock


class _KISModifyClient:
    def __init__(self, open_orders):
        self._open_orders = open_orders
        self.modify_korea_order = AsyncMock(return_value={"odno": "NEW1"})
        self.modify_overseas_order = AsyncMock(return_value={"odno": "NEW2"})

    async def inquire_korea_orders(self, is_mock=False):
        return self._open_orders


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_domestic_live_sell_reprice_below_floor_blocked(
    monkeypatch,
) -> None:
    _patch_modify_holdings(monkeypatch, avg_price=90400.0)
    client = _KISModifyClient(
        [
            {
                "odno": "0001",
                "ord_unpr": "95000",
                "ord_qty": "10",
                "sll_buy_dvsn_cd": "01",  # sell
                "ord_gno_brno": "06010",
            }
        ]
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    result = await orders_modify_cancel._modify_kis_domestic(
        "0001", "375500", "equity_kr", 68000.0, None, False, is_mock=False
    )
    assert result["success"] is False
    assert "modify blocked" in result["error"].lower()
    client.modify_korea_order.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_domestic_live_buy_reprice_allowed(monkeypatch) -> None:
    holdings_mock = _patch_modify_holdings(monkeypatch, avg_price=90400.0)
    client = _KISModifyClient(
        [
            {
                "odno": "0002",
                "ord_unpr": "70000",
                "ord_qty": "10",
                "sll_buy_dvsn_cd": "02",  # buy
                "ord_gno_brno": "06010",
            }
        ]
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    result = await orders_modify_cancel._modify_kis_domestic(
        "0002", "375500", "equity_kr", 68000.0, None, False, is_mock=False
    )
    assert result["success"] is True
    client.modify_korea_order.assert_called_once()
    holdings_mock.assert_not_called()  # buy modifies never touch holdings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_domestic_live_sell_reprice_above_floor_allowed(
    monkeypatch,
) -> None:
    _patch_modify_holdings(monkeypatch, avg_price=60000.0)
    client = _KISModifyClient(
        [
            {
                "odno": "0003",
                "ord_unpr": "70000",
                "ord_qty": "10",
                "sll_buy_dvsn_cd": "01",
                "ord_gno_brno": "06010",
            }
        ]
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    result = await orders_modify_cancel._modify_kis_domestic(
        "0003", "375500", "equity_kr", 68000.0, None, False, is_mock=False
    )
    assert result["success"] is True
    client.modify_korea_order.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_domestic_quantity_only_modify_skips_floor(
    monkeypatch,
) -> None:
    # Quantity-only modifies (new_price=None) introduce no new price risk.
    holdings_mock = _patch_modify_holdings(monkeypatch, avg_price=90400.0)
    client = _KISModifyClient(
        [
            {
                "odno": "0004",
                "ord_unpr": "95000",
                "ord_qty": "10",
                "sll_buy_dvsn_cd": "01",
                "ord_gno_brno": "06010",
            }
        ]
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    result = await orders_modify_cancel._modify_kis_domestic(
        "0004", "375500", "equity_kr", None, 5, False, is_mock=False
    )
    assert result["success"] is True
    holdings_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_overseas_live_sell_reprice_below_floor_blocked(
    monkeypatch,
) -> None:
    _patch_modify_holdings(monkeypatch, avg_price=300.0)
    client = _KISModifyClient([])
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    target_order = {
        "ft_ord_unpr3": "310.0",
        "ft_ord_qty": "10",
        "sll_buy_dvsn_cd": "01",  # sell
    }
    monkeypatch.setattr(
        orders_modify_cancel,
        "_find_us_open_order_by_id",
        AsyncMock(return_value=(target_order, "NASD", ["NASD"])),
    )
    result = await orders_modify_cancel._modify_kis_overseas(
        "0005", "AAPL", "equity_us", 250.0, None, False, is_mock=False
    )
    assert result["success"] is False
    assert "modify blocked" in result["error"].lower()
    client.modify_overseas_order.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_upbit_sell_reprice_below_floor_blocked(monkeypatch) -> None:
    _patch_modify_holdings(monkeypatch, avg_price=40000000.0)
    monkeypatch.setattr(
        orders_modify_cancel.upbit_service,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "state": "wait",
                "ord_type": "limit",
                "side": "ask",  # sell
                "price": "41000000",
                "remaining_volume": "0.1",
            }
        ),
    )
    reorder = AsyncMock()
    monkeypatch.setattr(
        orders_modify_cancel.upbit_service, "cancel_and_reorder", reorder
    )
    result = await orders_modify_cancel._modify_upbit(
        "uuid-1", "KRW-BTC", "crypto", 31000000.0, None, False
    )
    assert result["success"] is False
    assert "modify blocked" in result["error"].lower()
    reorder.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_upbit_buy_reprice_allowed(monkeypatch) -> None:
    holdings_mock = _patch_modify_holdings(monkeypatch, avg_price=40000000.0)
    monkeypatch.setattr(
        orders_modify_cancel.upbit_service,
        "fetch_order_detail",
        AsyncMock(
            return_value={
                "state": "wait",
                "ord_type": "limit",
                "side": "bid",  # buy
                "price": "39000000",
                "remaining_volume": "0.1",
            }
        ),
    )
    reorder = AsyncMock(return_value={"new_order": {"uuid": "uuid-2"}})
    monkeypatch.setattr(
        orders_modify_cancel.upbit_service, "cancel_and_reorder", reorder
    )
    result = await orders_modify_cancel._modify_upbit(
        "uuid-1", "KRW-BTC", "crypto", 31000000.0, None, False
    )
    assert result["success"] is True
    reorder.assert_called_once()
    holdings_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_unknown_basis_fails_open(monkeypatch) -> None:
    # Holdings without an avg_price (or missing entirely) must not brick modify.
    _patch_modify_holdings(monkeypatch, avg_price=None)
    client = _KISModifyClient(
        [
            {
                "odno": "0006",
                "ord_unpr": "95000",
                "ord_qty": "10",
                "sll_buy_dvsn_cd": "01",
                "ord_gno_brno": "06010",
            }
        ]
    )
    monkeypatch.setattr(
        orders_modify_cancel, "_create_kis_client", lambda *, is_mock: client
    )
    result = await orders_modify_cancel._modify_kis_domestic(
        "0006", "375500", "equity_kr", 68000.0, None, False, is_mock=False
    )
    assert result["success"] is True
    client.modify_korea_order.assert_called_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_modify_kis_mock_domestic_skips_floor(monkeypatch) -> None:
    # Mock modifies delegate before the live guard (ROB-461 practice semantics).
    delegate = AsyncMock(return_value={"success": True, "status": "modified"})
    monkeypatch.setattr(orders_modify_cancel, "_modify_kis_mock_domestic", delegate)
    holdings_mock = _patch_modify_holdings(monkeypatch, avg_price=90400.0)
    result = await orders_modify_cancel._modify_kis_domestic(
        "0007", "375500", "equity_kr", 68000.0, None, False, is_mock=True
    )
    assert result["success"] is True
    delegate.assert_called_once()
    holdings_mock.assert_not_called()
