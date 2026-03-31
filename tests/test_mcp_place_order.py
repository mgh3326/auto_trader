"""
Tests for place_order MCP tool.

This module contains all tests related to the place_order tool,
extracted from test_mcp_server_tools.py for better organization.
"""

import logging
from unittest.mock import AsyncMock

import pytest

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.tooling import order_execution
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    build_tools,
)

# ----------------------------------------------------------------------
# Amount-based order tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_with_amount_crypto_market_buy(monkeypatch):
    tools = build_tools()

    mock = AsyncMock()
    mock.fetch_multiple_current_prices = AsyncMock(return_value={"KRW-BTC": 50000000.0})
    mock.fetch_my_coins = AsyncMock(
        return_value=[{"currency": "KRW", "balance": "500000", "locked": "0"}]
    )
    mock.place_market_buy_order = AsyncMock(
        return_value={
            "uuid": "test-uuid",
            "side": "bid",
            "market": "KRW-BTC",
            "ord_type": "price",
            "price": "100000",
            "volume": "0.002",
        }
    )

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        mock.fetch_multiple_current_prices,
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        mock.fetch_my_coins,
    )
    monkeypatch.setattr(
        upbit_service,
        "place_market_buy_order",
        mock.place_market_buy_order,
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=100000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["quantity"] > 0


@pytest.mark.asyncio
async def test_place_order_with_amount_limit_order(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 50000000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[{"currency": "KRW", "balance": "500000", "locked": "0"}]
        ),
    )
    monkeypatch.setattr(
        upbit_service,
        "place_buy_order",
        AsyncMock(return_value={"uuid": "test-uuid", "side": "bid"}),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        amount=100000.0,
        price=49000000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["quantity"] == pytest.approx(100000.0 / 49000000.0, rel=1e-6)


@pytest.mark.asyncio
async def test_place_order_with_amount_stock_market_buy(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            return {"odno": "12345", "ord_qty": quantity}

        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000",
                "stck_cash_objt_amt": "5000000",
                "stck_itgr_cash100_ord_psbl_amt": "5000000",
            }

    async def fetch_quote(symbol):
        return {"price": 100000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="market",
        amount=1000000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["quantity"] == 10


@pytest.mark.asyncio
async def test_place_order_amount_and_quantity_both_error():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="amount and quantity cannot both be specified"
    ):
        await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="market",
            amount=100000.0,
            quantity=0.001,
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_place_order_sell_with_amount_error():
    tools = build_tools()

    with pytest.raises(ValueError, match="amount can only be used for buy orders"):
        await tools["place_order"](
            symbol="KRW-BTC",
            side="sell",
            order_type="market",
            amount=100000.0,
            dry_run=True,
        )


# ----------------------------------------------------------------------
# Upbit order tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_upbit_buy_limit_dry_run(monkeypatch):
    """Test Upbit buy limit order in dry_run mode."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "KRW", "balance": 2000000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "side": "buy",
                "order_type": "limit",
                "price": 45000000.0,
                "quantity": 0.02,
                "estimated_value": 900000.0,
                "fee": 4500.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        quantity=0.02,
        price=45000000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["symbol"] == "KRW-BTC"
    assert result["side"] == "buy"
    assert result["order_type"] == "limit"
    assert result["price"] == 45000000.0
    assert result["quantity"] == 0.02
    assert result["estimated_value"] == 900000.0
    assert result["fee"] == 4500.0


@pytest.mark.asyncio
async def test_place_order_upbit_buy_market_dry_run(monkeypatch):
    """Test Upbit buy market order in dry_run mode."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "KRW", "balance": 2000000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "side": "buy",
                "order_type": "market",
                "price": 50000000.0,
                "quantity": 0.04,
                "estimated_value": 2000000.0,
                "fee": 10000.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["order_type"] == "market"
    assert result["price"] == 50000000.0
    assert result["quantity"] == 0.04


@pytest.mark.asyncio
async def test_place_order_sell_limit_price_below_minimum(monkeypatch):
    """Test that sell limit order below 1% minimum is rejected."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "BTC", "balance": 0.1, "avg_buy_price": 40000000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 50000.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=0.1,
        price=39600000.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert "error" in result
    assert "below minimum" in result["error"]


@pytest.mark.asyncio
async def test_place_order_market_buy_calculates_quantity(monkeypatch):
    """Test that market buy order calculates quantity correctly."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "KRW", "balance": 2000000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["order_type"] == "market"
    assert result["quantity"] == 0.04
    assert result["price"] == 50000000.0
    assert result["estimated_value"] == 2000000.0


@pytest.mark.asyncio
async def test_place_order_market_sell_uses_full_quantity(monkeypatch):
    """Test that market sell order uses full holdings."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "BTC", "balance": 0.5, "avg_buy_price": 40000000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 25000000.0,
                "realized_pnl": 5000000.0,
                "avg_buy_price": 40000000.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["order_type"] == "market"
    assert result["quantity"] == 0.5
    assert result["realized_pnl"] == 5000000.0


# ----------------------------------------------------------------------
# Insufficient balance tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_insufficient_balance_upbit(monkeypatch):
    """Test that buying with insufficient Upbit balance shows deposit guidance."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [{"currency": "KRW", "balance": 50000.0}]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        quantity=0.1,
        price=50000000.0,
        dry_run=True,
    )

    assert result["success"] is True, (
        f"Expected success=True with warning, got {result}"
    )
    assert result["dry_run"] is True
    assert "warning" in result
    assert "Insufficient" in result["warning"]
    assert "deposit" in result["warning"].lower()
    assert "Upbit" in result["warning"]


@pytest.mark.asyncio
async def test_place_order_insufficient_balance_kis_domestic(monkeypatch):
    """Test that buying with insufficient KIS domestic balance shows deposit guidance."""
    tools = build_tools()

    class DummyKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "100000.0",
                "stck_cash_objt_amt": "100000.0",
                "stck_itgr_cash100_ord_psbl_amt": "100000.0",
            }

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 5000000.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=5000000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["journal_created"] is False
    assert "journal_warning" in result


@pytest.mark.asyncio
async def test_real_sell_closes_journals_and_returns_summary(monkeypatch) -> None:
    """Real sell should close journals and return summary in response."""
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "balance": "0.01",
                    "locked": "0",
                    "avg_buy_price": "90000000",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        upbit_service,
        "place_market_sell_order",
        AsyncMock(
            return_value={"uuid": "sell-uuid", "side": "ask", "market": "KRW-BTC"}
        ),
    )
    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=123)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

    close_mock = AsyncMock(
        return_value={
            "journals_closed": 2,
            "journals_kept": 1,
            "closed_ids": [42, 55],
            "total_pnl_pct": 5.2,
        }
    )
    monkeypatch.setattr(order_execution, "_close_journals_on_sell", close_mock)

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.01,
        dry_run=False,
        reason="rebalance",
        exit_reason="take_profit",
    )

    assert result["success"] is True
    assert result["journals_closed"] == 2
    assert result["journals_kept"] == 1
    assert result["closed_journal_ids"] == [42, 55]
    close_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_sell_journal_close_failure_keeps_order_success(monkeypatch) -> None:
    """Journal close failure should not fail the sell order."""
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "balance": "0.01",
                    "locked": "0",
                    "avg_buy_price": "90000000",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        upbit_service,
        "place_market_sell_order",
        AsyncMock(
            return_value={"uuid": "sell-uuid", "side": "ask", "market": "KRW-BTC"}
        ),
    )
    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=123)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())
    monkeypatch.setattr(
        order_execution,
        "_close_journals_on_sell",
        AsyncMock(side_effect=RuntimeError("db timeout")),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.01,
        dry_run=False,
    )

    assert result["success"] is True
    assert "journal_warning" in result
    assert "journal close failed after sell" in result["journal_warning"]


@pytest.mark.asyncio
async def test_sell_dry_run_does_not_close_journals(monkeypatch) -> None:
    """Dry run sell should not close journals."""
    tools = build_tools()

    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "balance": "0.01",
                    "locked": "0",
                    "avg_buy_price": "90000000",
                }
            ]
        ),
    )

    close_mock = AsyncMock()
    monkeypatch.setattr(order_execution, "_close_journals_on_sell", close_mock)

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.01,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    close_mock.assert_not_awaited()


class TestOrderFillRecording:
    """Tests for automatic recording of order fills to review.trades."""

    @pytest.mark.asyncio
    async def test_real_buy_requires_thesis_before_execution(self, monkeypatch) -> None:
        """Real buy orders must have thesis before execution."""
        tools = build_tools()
        preview_mock = AsyncMock()
        place_buy_mock = AsyncMock()
        monkeypatch.setattr(order_execution, "_preview_order", preview_mock)
        monkeypatch.setattr(upbit_service, "place_buy_order", place_buy_mock)

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=False,
            strategy="breakout",
        )

        assert result["success"] is False
        assert result["error"] == "thesis is required for buy orders when dry_run=False"
        preview_mock.assert_not_awaited()
        place_buy_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_buy_requires_strategy_before_execution(
        self, monkeypatch
    ) -> None:
        """Real buy orders must have strategy before execution."""
        tools = build_tools()
        preview_mock = AsyncMock()
        place_buy_mock = AsyncMock()
        monkeypatch.setattr(order_execution, "_preview_order", preview_mock)
        monkeypatch.setattr(upbit_service, "place_buy_order", place_buy_mock)

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=False,
            thesis="Breakout above resistance",
        )

        assert result["success"] is False
        assert (
            result["error"] == "strategy is required for buy orders when dry_run=False"
        )
        preview_mock.assert_not_awaited()
        place_buy_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_buy_allows_missing_thesis_and_strategy(
        self, monkeypatch
    ) -> None:
        """Dry-run buy orders can work without thesis/strategy."""
        tools = build_tools()
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"}
                ]
            ),
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_successful_order_saves_fill(self, monkeypatch) -> None:
        """Real order execution should save to review.trades."""
        tools = build_tools()

        # Mock Upbit API calls
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"}
                ]
            ),
        )
        monkeypatch.setattr(
            upbit_service,
            "place_buy_order",
            AsyncMock(
                return_value={
                    "uuid": "test-fill-uuid",
                    "side": "bid",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        # Mock the save function to capture the call
        save_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_save_order_fill",
            save_mock,
        )

        # Mock journal link
        link_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_link_journal_to_fill",
            link_mock,
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95000000.0,
            quantity=0.001,
            dry_run=False,
            reason="test fill",
            thesis="Test thesis for BTC",
            strategy="test-strategy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result.get("fill_recorded") is True
        save_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_save_fill(self, monkeypatch) -> None:
        """Dry-run orders should NOT save to review.trades."""
        tools = build_tools()

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95000000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"}
                ]
            ),
        )

        save_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution,
            "_save_order_fill",
            save_mock,
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95000000.0,
            quantity=0.001,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        save_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_buy_creates_journal_then_links_fill(self, monkeypatch) -> None:
        """Real buy orders should create journal and link to fill."""
        tools = build_tools()

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"}
                ]
            ),
        )
        monkeypatch.setattr(
            upbit_service,
            "place_buy_order",
            AsyncMock(
                return_value={
                    "uuid": "test-uuid",
                    "side": "bid",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        create_journal_mock = AsyncMock(
            return_value={
                "journal_created": True,
                "journal_id": 77,
                "journal_status": "draft",
            }
        )
        monkeypatch.setattr(
            order_execution, "_create_trade_journal_for_buy", create_journal_mock
        )
        monkeypatch.setattr(
            order_execution, "_save_order_fill", AsyncMock(return_value=555)
        )
        monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=False,
            thesis="Breakout above weekly resistance",
            strategy="weekly-breakout",
        )

        assert result["success"] is True
        assert result["journal_created"] is True
        assert result["journal_id"] == 77
        assert result["journal_status"] == "active"
        create_journal_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sell_order_does_not_create_trade_journal(self, monkeypatch) -> None:
        """Sell orders should not create trade journals."""
        tools = build_tools()

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"},
                    {"currency": "BTC", "balance": "0.01", "locked": "0"},
                ]
            ),
        )
        monkeypatch.setattr(
            upbit_service,
            "place_sell_order",
            AsyncMock(
                return_value={
                    "uuid": "test-uuid",
                    "side": "ask",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        create_journal_mock = AsyncMock()
        monkeypatch.setattr(
            order_execution, "_create_trade_journal_for_buy", create_journal_mock
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="sell",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=False,
        )

        assert result["success"] is True
        create_journal_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_journal_creation_failure_keeps_order_success(
        self, monkeypatch
    ) -> None:
        """Journal creation failure should not fail the order."""
        tools = build_tools()

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            AsyncMock(return_value={"KRW-BTC": 95_000_000.0}),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000000", "locked": "0"}
                ]
            ),
        )
        monkeypatch.setattr(
            upbit_service,
            "place_buy_order",
            AsyncMock(
                return_value={
                    "uuid": "test-uuid",
                    "side": "bid",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        monkeypatch.setattr(
            order_execution,
            "_create_trade_journal_for_buy",
            AsyncMock(side_effect=RuntimeError("db down")),
        )
        monkeypatch.setattr(
            order_execution, "_save_order_fill", AsyncMock(return_value=555)
        )
        monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            price=95_000_000.0,
            quantity=0.001,
            dry_run=False,
            thesis="Breakout above weekly resistance",
            strategy="weekly-breakout",
        )

        assert result["success"] is True
        assert result["journal_created"] is False
        assert "journal_warning" in result
