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
    assert result["dry_run"] is True
    assert "warning" in result
    assert "Insufficient" in result["warning"]
    assert "KIS domestic account" in result["warning"]


@pytest.mark.asyncio
async def test_place_order_insufficient_balance_kis_domestic_blocks_real_order(
    monkeypatch,
):
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
        dry_run=False,
        thesis="Test thesis",
        strategy="test-strategy",
    )

    assert result["success"] is False
    assert "dry_run" not in result
    assert "Insufficient" in result["error"]
    assert "KIS domestic account" in result["error"]


@pytest.mark.asyncio
async def test_place_order_insufficient_balance_kis_overseas(monkeypatch):
    """Test that buying with insufficient KIS overseas balance shows deposit guidance."""
    tools = build_tools()

    class DummyKISClient:
        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "100.0",
                    "frcr_gnrl_ord_psbl_amt": "100.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 500.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        price=500.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert "warning" in result
    assert "Insufficient" in result["warning"]
    assert "KIS overseas account" in result["warning"]
    assert "deposit" in result["warning"].lower()
    assert "100.00 USD < 500.00 USD" in result["warning"]


# ----------------------------------------------------------------------
# US equity order tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_us_dry_run_uses_overseas_margin_only(monkeypatch):
    """US dry_run 잔고 조회는 해외증거금만 사용한다."""
    tools = build_tools()
    integrated_called = False

    class DummyKISClient:
        async def inquire_integrated_margin(self):
            nonlocal integrated_called
            integrated_called = True
            return {"usd_ord_psbl_amt": "999999.0"}

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "2000.0",
                    "frcr_gnrl_ord_psbl_amt": "1500.0",
                }
            ]

    async def fetch_quote(symbol):
        return {"price": 400.0}

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", fetch_quote)

    result = await tools["place_order"](
        symbol="MSFT",
        side="buy",
        order_type="limit",
        quantity=1,
        price=400.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert integrated_called is False


@pytest.mark.asyncio
async def test_place_order_us_uses_frcr_gnrl_orderable_when_ord1_is_zero(monkeypatch):
    """ord1이 0이어도 frcr_gnrl_ord_psbl_amt를 사용해 잔고를 판단한다."""
    tools = build_tools()

    class DummyKISClient:
        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "120.0",
                    "frcr_ord_psbl_amt1": "0.0",
                    "frcr_gnrl_ord_psbl_amt": "100.0",
                }
            ]

    async def fetch_quote(symbol):
        return {"price": 500.0}

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", fetch_quote)
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 500.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="AAPL",
        side="buy",
        order_type="limit",
        quantity=1,
        price=500.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert "warning" in result
    assert "100.00 USD < 500.00 USD" in result["warning"]


@pytest.mark.asyncio
async def test_place_order_balance_lookup_failure_returns_query_error(
    monkeypatch, caplog
):
    """Balance lookup failure should return API error, not 0-balance warning."""
    tools = build_tools()

    class FailingKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("KIS integrated margin lookup failed")

    async def fetch_quote(symbol):
        return {"price": 5000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", FailingKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    with caplog.at_level(logging.ERROR):
        result = await tools["place_order"](
            symbol="005930",
            side="buy",
            order_type="limit",
            quantity=1,
            price=5000.0,
            dry_run=True,
        )

    assert result["success"] is False
    assert "KIS integrated margin lookup failed" in result["error"]
    assert "Insufficient KRW balance: 0 KRW" not in result["error"]
    assert any("stage=balance_query" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_place_order_nyse_exchange_code(monkeypatch):
    """Test that NYSE stocks (e.g. TSM) use correct exchange code instead of hardcoded NASD."""
    tools = build_tools()

    buy_calls: list[dict[str, object]] = []

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "usd_ord_psbl_amt": "100000",
                "dnca_tot_amt": "0",
                "stck_cash_ord_psbl_amt": "0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "100000",
                    "frcr_gnrl_ord_psbl_amt": "100000",
                }
            ]

        async def buy_overseas_stock(self, symbol, exchange_code, quantity, price):
            buy_calls.append(
                {
                    "symbol": symbol,
                    "exchange_code": exchange_code,
                    "quantity": quantity,
                    "price": price,
                }
            )
            return {"odno": "99999", "success": True}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=lambda s: "NYSE" if s == "TSM" else None),
    )

    result = await tools["place_order"](
        symbol="TSM",
        side="buy",
        order_type="limit",
        quantity=10,
        price=150.0,
        dry_run=False,
        thesis="Test thesis for TSM",
        strategy="test-strategy",
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert len(buy_calls) == 1
    assert buy_calls[0]["symbol"] == "TSM"
    assert buy_calls[0]["exchange_code"] == "NYSE"
    assert buy_calls[0]["quantity"] == 10
    assert buy_calls[0]["price"] == 150.0


@pytest.mark.asyncio
async def test_kis_overseas_order_payload_fields_buy(monkeypatch):
    del monkeypatch
    import inspect

    from app.services.brokers.kis.client import KISClient

    sig = inspect.signature(KISClient.order_overseas_stock)
    params = list(sig.parameters.keys())

    assert "symbol" in params
    assert "exchange_code" in params
    assert "order_type" in params
    assert "quantity" in params
    assert "price" in params


# ---------------------------------------------------------------------------
# High-amount order tests
# ---------------------------------------------------------------------------


class TestPlaceOrderHighAmount:
    """Tests for place_order with high-amount orders."""

    @pytest.mark.asyncio
    async def test_get_current_price_for_order_crypto_bypasses_ticker_cache(
        self, monkeypatch
    ):
        ticker_mock = AsyncMock(return_value={"KRW-BTC": 50000000.0})
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            ticker_mock,
        )

        price = await order_execution._get_current_price_for_order("KRW-BTC", "crypto")

        assert price == 50000000.0
        ticker_mock.assert_awaited_once_with(["KRW-BTC"], use_cache=False)

    @pytest.mark.asyncio
    async def test_place_order_high_amount_kr_equity(self, monkeypatch):
        """place_order accepts high-amount orders (> 1M KRW) for KR equity."""
        tools = build_tools()

        class MockKISClient:
            async def inquire_integrated_margin(self):
                return {
                    "dnca_tot_amt": "100000000.0",
                    "stck_cash_objt_amt": "100000000.0",
                    "stck_itgr_cash100_ord_psbl_amt": "100000000.0",
                }

        async def fetch_quote(symbol):
            return {"price": 100000.0}

        _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

        result = await tools["place_order"](
            symbol="005930",
            side="buy",
            order_type="market",
            amount=5_000_000.0,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["quantity"] == 50

    @pytest.mark.asyncio
    async def test_place_order_high_amount_us_equity(self, monkeypatch):
        """place_order accepts high-amount orders for US equity."""
        tools = build_tools()

        class MockKISClient:
            async def inquire_integrated_margin(self):
                return {
                    "usd_ord_psbl_amt": "3000000.0",
                    "usd_balance": "3000000.0",
                    "dnca_tot_amt": "0",
                }

            async def inquire_overseas_margin(self):
                return [
                    {
                        "natn_name": "미국",
                        "crcy_cd": "USD",
                        "frcr_dncl_amt1": "3000000.0",
                        "frcr_gnrl_ord_psbl_amt": "3000000.0",
                    }
                ]

        async def fetch_quote(symbol):
            return {"price": 205.0}

        _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", fetch_quote)

        result = await tools["place_order"](
            symbol="AAPL",
            side="buy",
            order_type="market",
            amount=2_600_000.0,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_place_order_high_amount_crypto(self, monkeypatch):
        """place_order accepts high-amount orders for crypto."""
        tools = build_tools()

        mock = AsyncMock()
        mock.fetch_multiple_current_prices = AsyncMock(
            return_value={"KRW-BTC": 50000000.0}
        )
        mock.fetch_my_coins = AsyncMock(
            return_value=[{"currency": "KRW", "balance": "10000000", "locked": "0"}]
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

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="market",
            amount=5_000_000.0,
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["quantity"] > 0

    @pytest.mark.asyncio
    async def test_place_order_high_amount_kr_equity_dry_run_false(self, monkeypatch):
        """High-amount KR equity order executes when dry_run=False."""
        tools = build_tools()

        order_calls: list[dict[str, object]] = []

        class MockKISClient:
            async def inquire_integrated_margin(self):
                return {
                    "dnca_tot_amt": "100000000.0",
                    "stck_cash_objt_amt": "100000000.0",
                    "stck_itgr_cash100_ord_psbl_amt": "100000000.0",
                }

            async def order_korea_stock(self, stock_code, order_type, quantity, price):
                order_calls.append(
                    {
                        "stock_code": stock_code,
                        "order_type": order_type,
                        "quantity": quantity,
                        "price": price,
                    }
                )
                return {"odno": "kr-12345", "ord_qty": quantity}

        async def fetch_quote(symbol):
            return {"price": 120000.0}

        _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

        result = await tools["place_order"](
            symbol="005930",
            side="buy",
            order_type="limit",
            amount=5_500_000.0,
            price=110000.0,
            dry_run=False,
            thesis="Test thesis for Samsung",
            strategy="test-strategy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["preview"]["quantity"] == 50
        assert len(order_calls) == 1
        assert order_calls[0]["stock_code"] == "005930"
        assert order_calls[0]["order_type"] == "buy"
        assert order_calls[0]["quantity"] == 50
        assert order_calls[0]["price"] == 110000

    @pytest.mark.asyncio
    async def test_place_order_high_amount_us_equity_dry_run_false(self, monkeypatch):
        """High-amount US equity order executes when dry_run=False."""
        tools = build_tools()

        buy_calls: list[dict[str, object]] = []

        class MockKISClient:
            async def inquire_integrated_margin(self):
                return {
                    "stck_cash_ord_psbl_amt": "0",
                    "usd_ord_psbl_amt": "3000000.0",
                    "dnca_tot_amt": "0",
                }

            async def inquire_overseas_margin(self):
                return [
                    {
                        "natn_name": "미국",
                        "crcy_cd": "USD",
                        "frcr_dncl_amt1": "3000000.0",
                        "frcr_gnrl_ord_psbl_amt": "3000000.0",
                    }
                ]

            async def buy_overseas_stock(self, symbol, exchange_code, quantity, price):
                buy_calls.append(
                    {
                        "symbol": symbol,
                        "exchange_code": exchange_code,
                        "quantity": quantity,
                        "price": price,
                    }
                )
                return {"odno": "us-12345", "success": True}

        async def fetch_quote(symbol):
            return {"price": 250.0}

        _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", fetch_quote)
        _patch_runtime_attr(
            monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
        )

        result = await tools["place_order"](
            symbol="AAPL",
            side="buy",
            order_type="limit",
            amount=2_500_000.0,
            price=250.0,
            dry_run=False,
            thesis="Test thesis for AAPL",
            strategy="test-strategy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["preview"]["quantity"] == 10000
        assert len(buy_calls) == 1
        assert buy_calls[0]["symbol"] == "AAPL"
        assert buy_calls[0]["exchange_code"] == "NASD"
        assert buy_calls[0]["quantity"] == 10000
        assert buy_calls[0]["price"] == 250.0

    @pytest.mark.asyncio
    async def test_place_order_high_amount_crypto_dry_run_false(self, monkeypatch):
        """High-amount crypto order executes when dry_run=False."""
        tools = build_tools()

        mock = AsyncMock()
        mock.fetch_multiple_current_prices = AsyncMock(
            return_value={"KRW-BTC": 50000000.0}
        )
        mock.fetch_my_coins = AsyncMock(
            return_value=[{"currency": "KRW", "balance": "10000000", "locked": "0"}]
        )
        mock.place_market_buy_order = AsyncMock(
            return_value={"uuid": "crypto-12345", "side": "bid"}
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
            amount=5_000_000.0,
            dry_run=False,
            thesis="Test thesis for BTC",
            strategy="test-strategy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["preview"]["quantity"] == pytest.approx(0.1, rel=1e-6)
        mock.place_market_buy_order.assert_awaited_once_with("KRW-BTC", "5000000")

    @pytest.mark.asyncio
    async def test_place_order_daily_limit_blocks_high_amount_order(self, monkeypatch):
        """Daily order limit is enforced even for high-amount orders."""
        tools = build_tools()

        mock = AsyncMock()
        mock.fetch_multiple_current_prices = AsyncMock(
            return_value={"KRW-BTC": 50000000.0}
        )
        mock.fetch_my_coins = AsyncMock(
            return_value=[{"currency": "KRW", "balance": "10000000", "locked": "0"}]
        )
        mock.place_market_buy_order = AsyncMock(
            return_value={"uuid": "crypto-12345", "side": "bid"}
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
        monkeypatch.setattr(
            settings, "redis_url", "redis://localhost:6379/0", raising=False
        )

        class FakeRedisClient:
            async def get(self, key):
                return "20"

        monkeypatch.setattr(
            "redis.asyncio.from_url", AsyncMock(return_value=FakeRedisClient())
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="market",
            amount=5_000_000.0,
            dry_run=False,
            thesis="Test thesis for BTC",
            strategy="test-strategy",
        )

        assert result["success"] is False
        assert "Daily order limit" in result.get(
            "error", ""
        ) or "Daily order limit" in result.get("message", "")


# ----------------------------------------------------------------------
# KR tick adjustment tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_kr_limit_keeps_valid_tick_without_adjustment_metadata(
    monkeypatch,
):
    """KR limit order with valid tick price should not include tick_adjusted metadata."""
    tools = build_tools()

    class MockKISClient:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            return {"odno": "12345", "ord_qty": quantity, "ord_unpr": price}

        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "50000000",
                "stck_cash_objt_amt": "50000000",
                "stck_itgr_cash100_ord_psbl_amt": "50000000",
            }

    async def fetch_quote(symbol):
        return {"price": 1_100_000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=1_098_000,
        dry_run=False,
        thesis="Test thesis for Samsung",
        strategy="test-strategy",
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["preview"]["symbol"] == "005930"
    assert result["preview"]["side"] == "buy"
    assert result["preview"]["quantity"] == 10
    assert result["preview"]["price"] == 1_098_000

    assert "tick_adjusted" not in result["execution"]
    assert "original_price" not in result["execution"]
    assert "adjusted_price" not in result["execution"]


@pytest.mark.asyncio
async def test_place_order_kr_limit_applies_tick_adjustment_and_metadata(
    monkeypatch, caplog
):
    """KR limit order with invalid tick price should adjust and include metadata."""
    caplog.set_level(logging.DEBUG)

    tools = build_tools()

    class MockKISClient:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            return {"odno": "67890", "ord_qty": quantity, "ord_unpr": price}

        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "50000000",
                "stck_cash_objt_amt": "50000000",
                "stck_itgr_cash100_ord_psbl_amt": "50000000",
            }

    async def fetch_quote(symbol):
        return {"price": 1_100_000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=1_098_500,
        dry_run=False,
        thesis="Test thesis for Samsung",
        strategy="test-strategy",
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert result["preview"]["symbol"] == "005930"
    assert result["preview"]["side"] == "buy"
    assert result["preview"]["quantity"] == 10

    assert result["execution"]["tick_adjusted"] is True
    assert result["execution"]["original_price"] == 1_098_500
    assert result["execution"]["adjusted_price"] == 1_098_000

    info_records = [
        record for record in caplog.records if record.levelno >= logging.INFO
    ]
    info_messages = [record.message for record in info_records]

    adjustment_logged = any(
        "tick adjusted" in msg.lower() and "1098500" in msg for msg in info_messages
    )
    assert adjustment_logged, f"Expected tick adjustment log, got: {info_messages}"


# ----------------------------------------------------------------------
# KR integrated-margin precheck
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_kr_equity_calls_integrated_margin(monkeypatch):
    tools = build_tools()

    integrated_margin_called = False
    domestic_called = False

    class MockKISClient:
        async def inquire_integrated_margin(self):
            nonlocal integrated_margin_called
            integrated_margin_called = True
            return {
                "dnca_tot_amt": "50000000.0",
                "stck_cash_objt_amt": "50000000.0",
                "stck_itgr_cash100_ord_psbl_amt": "0.0",
                "stck_cash100_max_ord_psbl_amt": "50000000.0",
                "raw": {
                    "dnca_tot_amt": "50000000.0",
                    "stck_cash_objt_amt": "50000000.0",
                    "stck_itgr_cash100_ord_psbl_amt": "0.0",
                    "stck_cash100_max_ord_psbl_amt": "50000000.0",
                },
            }

        async def inquire_domestic_cash_balance(self):
            nonlocal domestic_called
            domestic_called = True
            return {
                "stck_cash_ord_psbl_amt": "0.0",
                "dnca_tot_amt": "0.0",
            }

    async def fetch_quote(symbol):
        return {"price": 80000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="market",
        amount=1_000_000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert integrated_margin_called is True
    assert domestic_called is False
    assert "warning" not in result


@pytest.mark.asyncio
async def test_place_order_kr_equity_balance_precheck_skips_zero_priority_orderables(
    monkeypatch,
):
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_objt_amt": "5000000.0",
                "stck_cash100_max_ord_psbl_amt": "0",
                "stck_itgr_cash100_ord_psbl_amt": "0",
                "stck_cash_ord_psbl_amt": "2100000.25",
                "raw": {
                    "dnca_tot_amt": "5000000.0",
                    "stck_cash_objt_amt": "5000000.0",
                    "stck_cash100_max_ord_psbl_amt": "0",
                    "stck_itgr_cash100_ord_psbl_amt": "0",
                    "stck_cash_ord_psbl_amt": "2100000.25",
                },
            }

    async def fetch_quote(symbol):
        return {"price": 70000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=10,
        price=70000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["estimated_value"] == 700000.0
    assert "warning" not in result


@pytest.mark.asyncio
async def test_place_order_kr_equity_balance_lookup_failure_returns_query_error(
    monkeypatch, caplog
):
    """KR 주문 잔고 조회 실패는 잔고 조회 실패로 즉시 반환한다."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("OPSQ2001 CMA_EVLU_AMT_ICLD_YN error")

    async def fetch_quote(symbol):
        return {"price": 60000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    with caplog.at_level(logging.ERROR):
        result = await tools["place_order"](
            symbol="005930",
            side="buy",
            order_type="market",
            amount=500_000.0,
            dry_run=True,
        )

    assert result["success"] is False
    assert "OPSQ2001 CMA_EVLU_AMT_ICLD_YN error" in result["error"]
    assert any("stage=balance_query" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_place_order_kr_equity_balance_lookup_failure_blocks_real_order(
    monkeypatch,
):
    """dry_run=False에서도 통합증거금 조회 실패는 주문 자체를 차단한다."""
    tools = build_tools()

    order_calls: list[dict[str, object]] = []

    class MockKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("OPSQ2001 CMA_EVLU_AMT_ICLD_YN error")

        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            order_calls.append(
                {
                    "stock_code": stock_code,
                    "order_type": order_type,
                    "quantity": quantity,
                    "price": price,
                }
            )
            return {"odno": "kr-99999", "ord_qty": quantity}

    async def fetch_quote(symbol):
        return {"price": 70000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        amount=700_000.0,
        price=70000.0,
        dry_run=False,
        thesis="Test thesis for Samsung",
        strategy="test-strategy",
    )

    assert result["success"] is False
    assert "dry_run" not in result
    assert "OPSQ2001 CMA_EVLU_AMT_ICLD_YN error" in result["error"]
    assert len(order_calls) == 0


# ----------------------------------------------------------------------
# Crypto sell orderable vs locked regression tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_crypto_sell_exceeds_orderable_with_locked(monkeypatch):
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [
                {
                    "currency": "BTC",
                    "balance": 0.03,
                    "locked": 0.02,
                    "avg_buy_price": 50000000.0,
                }
            ]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.05,
        dry_run=True,
    )

    assert result["success"] is False
    assert "orderable balance 0.03" in result["error"]
    assert "locked=0.02" in result["error"]


@pytest.mark.asyncio
async def test_place_order_crypto_market_sell_uses_orderable_only(monkeypatch):
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [
                {
                    "currency": "BTC",
                    "balance": 0.03,
                    "locked": 0.02,
                    "avg_buy_price": 50000000.0,
                }
            ]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "estimated_value": 1500000.0,
                "realized_pnl": 0.0,
                "avg_buy_price": 50000000.0,
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
    assert result["quantity"] == 0.03


@pytest.mark.asyncio
async def test_place_order_crypto_sell_locked_zero_still_works(monkeypatch):
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 50000000.0}

        async def fetch_my_coins(self):
            return [
                {
                    "currency": "BTC",
                    "balance": 0.5,
                    "locked": 0,
                    "avg_buy_price": 40000000.0,
                }
            ]

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
    assert result["quantity"] == 0.5


# ------------------------------------------------------------------------------
# Crypto stop-loss cooldown order execution tests
# ------------------------------------------------------------------------------


class FakeCooldownService:
    """Fake cooldown service for testing."""

    def __init__(self, in_cooldown: bool = False):
        self._in_cooldown = in_cooldown
        self.recorded_symbols: list[str] = []

    async def is_in_cooldown(self, symbol: str) -> bool:
        return self._in_cooldown

    async def record_stop_loss(self, symbol: str) -> None:
        self.recorded_symbols.append(symbol)

    async def get_remaining_ttl_seconds(self, symbol: str) -> int | None:
        return 86400 if self._in_cooldown else None


@pytest.mark.asyncio
async def test_place_order_crypto_buy_blocked_by_stop_loss_cooldown(monkeypatch):
    """Test that crypto buy orders are blocked when symbol is in cooldown."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "_get_crypto_trade_cooldown_service",
        lambda: FakeCooldownService(in_cooldown=True),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=100000.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert "cooldown" in result.get("error", "").lower()


@pytest.mark.asyncio
async def test_place_order_crypto_sell_records_stop_loss_cooldown(monkeypatch):
    """Test that stop-loss sells record cooldown after successful execution."""
    tools = build_tools()

    fake_cooldown_service = FakeCooldownService(in_cooldown=False)

    _patch_runtime_attr(
        monkeypatch,
        "_get_crypto_trade_cooldown_service",
        lambda: fake_cooldown_service,
    )

    # Simulate holding with avg_buy_price that indicates stop-loss
    # Current price 45000000 < avg_price 50000000 * (1 - 0.045) = 47750000
    # So current price is below stop-loss threshold
    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 45000000.0}  # 10% below avg buy price

        async def fetch_my_coins(self):
            return [
                {"currency": "BTC", "balance": "0.5", "avg_buy_price": "50000000.0"}
            ]

        async def place_market_sell_order(self, symbol, volume):
            return {
                "uuid": "test-uuid",
                "side": "ask",
                "market": symbol,
                "volume": volume,
            }

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.5,
        dry_run=False,
    )

    assert result["success"] is True
    assert "KRW-BTC" in fake_cooldown_service.recorded_symbols


@pytest.mark.asyncio
async def test_place_order_crypto_dry_run_stop_loss_does_not_record_cooldown(
    monkeypatch,
):
    """Test that dry-run stop-loss sells do not record cooldown."""
    tools = build_tools()

    fake_cooldown_service = FakeCooldownService(in_cooldown=False)

    _patch_runtime_attr(
        monkeypatch,
        "_get_crypto_trade_cooldown_service",
        lambda: fake_cooldown_service,
    )

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 45000000.0}

        async def fetch_my_coins(self):
            return [
                {"currency": "BTC", "balance": "0.5", "avg_buy_price": "50000000.0"}
            ]

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())
    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "side": "sell",
                "order_type": "market",
                "price": 45000000.0,
                "quantity": 0.5,
                "estimated_value": 22500000.0,
                "fee": 11250.0,
                "avg_buy_price": 50000000.0,
            }
        ),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.5,
        dry_run=True,
    )

    assert result["success"] is True
    assert len(fake_cooldown_service.recorded_symbols) == 0


@pytest.mark.asyncio
async def test_place_order_crypto_profitable_sell_does_not_record_cooldown(monkeypatch):
    """Test that profitable sells above stop-loss threshold do not record cooldown."""
    tools = build_tools()

    fake_cooldown_service = FakeCooldownService(in_cooldown=False)

    _patch_runtime_attr(
        monkeypatch,
        "_get_crypto_trade_cooldown_service",
        lambda: fake_cooldown_service,
    )

    # Simulate holding with profit
    # Current price 55000000 > avg_price 50000000 (profit)
    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 55000000.0}  # 10% above avg buy price

        async def fetch_my_coins(self):
            return [
                {"currency": "BTC", "balance": "0.5", "avg_buy_price": "50000000.0"}
            ]

        async def place_market_sell_order(self, symbol, volume):
            return {
                "uuid": "test-uuid",
                "side": "ask",
                "market": symbol,
                "volume": volume,
            }

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    _patch_runtime_attr(
        monkeypatch,
        "_preview_order",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "side": "sell",
                "order_type": "market",
                "price": 55000000.0,
                "quantity": 0.5,
                "estimated_value": 27500000.0,
                "fee": 13750.0,
                "avg_buy_price": 50000000.0,
            }
        ),
    )

    # Mock _save_order_fill and _link_journal_to_fill
    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        quantity=0.5,
        dry_run=False,
    )

    assert result["success"] is True
    assert len(fake_cooldown_service.recorded_symbols) == 0


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
