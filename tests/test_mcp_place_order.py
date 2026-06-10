"""
Tests for place_order MCP tool.

This module contains all tests related to the place_order tool,
extracted from test_mcp_server_tools.py for better organization.
"""

import logging
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.mcp_server.tooling import (
    order_execution,
    order_validation,
    orders_kis_variants,
    orders_registration,
)
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    build_tools,
)

EXPECTED_MARKET_ERROR = (
    "MCP place_order only supports limit orders; market orders are not allowed."
)


def _unique_order_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


@pytest_asyncio.fixture(autouse=True)
async def _ensure_live_order_ledger_schema(db_session):
    """ROB-407: live US/crypto orders write to review.live_order_ledger directly.
    Depend on db_session so its create_all builds the table before any test in this
    module inserts (CI builds the test schema via create_all, not alembic)."""
    yield


def _assert_market_rejected(result):
    assert result["success"] is False, result
    assert result.get("error") == EXPECTED_MARKET_ERROR, result
    # must NOT look like a successful preview or execution
    assert result.get("dry_run") is not True
    assert "execution" not in result


# ----------------------------------------------------------------------
# Amount-based order tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_with_amount_crypto_market_buy(monkeypatch):
    """MCP place_order must reject crypto market buy."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=5_000_000,
        dry_run=False,
        thesis="t",
        strategy="s",
    )
    _assert_market_rejected(result)


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
    """MCP place_order must reject KR stock market buy."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="market",
        amount=1_000_000,
        dry_run=False,
        thesis="t",
        strategy="s",
    )
    _assert_market_rejected(result)


@pytest.mark.asyncio
async def test_place_order_amount_and_quantity_both_error():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="amount and quantity cannot both be specified"
    ):
        await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            amount=100000.0,
            quantity=0.001,
            price=50000000.0,
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_place_order_sell_with_amount_error():
    tools = build_tools()

    with pytest.raises(ValueError, match="amount can only be used for buy orders"):
        await tools["place_order"](
            symbol="KRW-BTC",
            side="sell",
            order_type="limit",
            price=50000000.0,
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
    assert result["price"] == pytest.approx(45000000.0)
    assert result["quantity"] == pytest.approx(0.02)
    assert result["estimated_value"] == pytest.approx(900000.0)
    assert result["fee"] == pytest.approx(4500.0)


@pytest.mark.asyncio
async def test_place_order_upbit_buy_market_dry_run(monkeypatch):
    """MCP place_order must reject crypto market buy even in dry_run."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=1_000_000,
        dry_run=True,
        thesis="t",
        strategy="s",
    )
    _assert_market_rejected(result)


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
    """MCP place_order must reject crypto market buy (calculates quantity path)."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=2_000_000,
        dry_run=True,
        thesis="t",
        strategy="s",
    )
    _assert_market_rejected(result)


@pytest.mark.asyncio
async def test_place_order_market_sell_uses_full_quantity(monkeypatch):
    """MCP place_order must reject KR stock market sell."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="sell",
        order_type="market",
        dry_run=False,
    )
    _assert_market_rejected(result)


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

        async def inquire_korea_orders(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    async def fetch_quote(symbol):
        assert symbol == "005930"
        return {"price": 5000000.0}

    _patch_runtime_attr(
        monkeypatch,
        "_premarket_nxt_price_for_kr",
        AsyncMock(return_value=None),
    )
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

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

        async def inquire_korea_orders(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    async def fetch_quote(symbol):
        assert symbol == "005930"
        return {"price": 5000000.0}

    _patch_runtime_attr(
        monkeypatch,
        "_premarket_nxt_price_for_kr",
        AsyncMock(return_value=None),
    )
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

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

        def adjust_price_to_upbit_unit(self, price):
            return price

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
            return {"odno": _unique_order_id("us-nyse"), "success": True}

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
    assert buy_calls[0]["price"] == pytest.approx(150.0)


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


class TestPremarketNxtPricing:
    """ROB-463: KR pre-market orders price off the live NXT orderbook, not the
    stale KRX previous close."""

    @staticmethod
    def _nxt_book(expected_price=None, asks=None, bids=None, empty=False):
        import app.services.market_data as market_data_service

        return market_data_service.OrderbookSnapshot(
            symbol="005930",
            instrument_type="equity_kr",
            source="kis",
            asks=[
                market_data_service.OrderbookLevel(price=p, quantity=q)
                for p, q in (asks or [])
            ],
            bids=[
                market_data_service.OrderbookLevel(price=p, quantity=q)
                for p, q in (bids or [])
            ],
            total_ask_qty=0.0,
            total_bid_qty=0.0,
            bid_ask_ratio=None,
            expected_price=expected_price,
            venue="nxt",
            is_empty_book=empty,
        )

    @pytest.mark.asyncio
    async def test_premarket_uses_nxt_expected_price(self, monkeypatch):
        from app.mcp_server.tooling import order_validation
        from app.mcp_server.tooling.market_session import (
            DATA_STATE_PREMARKET_UNAVAILABLE,
        )

        monkeypatch.setattr(
            order_validation,
            "kr_market_data_state",
            lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
        )
        monkeypatch.setattr(
            order_validation.market_data_service,
            "get_orderbook",
            AsyncMock(return_value=self._nxt_book(expected_price=114300)),
        )
        monkeypatch.setattr(
            order_validation,
            "_fetch_quote_equity_kr",
            AsyncMock(return_value={"price": 112400}),
        )

        price = await order_validation._get_current_price_for_order(
            "005930", "equity_kr"
        )
        assert price == pytest.approx(114300.0)

    @pytest.mark.asyncio
    async def test_premarket_uses_nxt_mid_when_no_expected(self, monkeypatch):
        from app.mcp_server.tooling import order_validation
        from app.mcp_server.tooling.market_session import (
            DATA_STATE_PREMARKET_UNAVAILABLE,
        )

        monkeypatch.setattr(
            order_validation,
            "kr_market_data_state",
            lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
        )
        monkeypatch.setattr(
            order_validation.market_data_service,
            "get_orderbook",
            AsyncMock(
                return_value=self._nxt_book(asks=[(114500, 10)], bids=[(114100, 10)])
            ),
        )
        monkeypatch.setattr(
            order_validation,
            "_fetch_quote_equity_kr",
            AsyncMock(return_value={"price": 112400}),
        )

        price = await order_validation._get_current_price_for_order(
            "005930", "equity_kr"
        )
        assert price == pytest.approx(114300.0)  # mid of 114500 / 114100

    @pytest.mark.asyncio
    async def test_premarket_empty_book_falls_back_to_krx(self, monkeypatch):
        from app.mcp_server.tooling import order_validation
        from app.mcp_server.tooling.market_session import (
            DATA_STATE_PREMARKET_UNAVAILABLE,
        )

        monkeypatch.setattr(
            order_validation,
            "kr_market_data_state",
            lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
        )
        monkeypatch.setattr(
            order_validation.market_data_service,
            "get_orderbook",
            AsyncMock(return_value=self._nxt_book(empty=True)),
        )
        monkeypatch.setattr(
            order_validation,
            "_fetch_quote_equity_kr",
            AsyncMock(return_value={"price": 112400}),
        )

        price = await order_validation._get_current_price_for_order(
            "005930", "equity_kr"
        )
        assert price == pytest.approx(112400.0)

    @pytest.mark.asyncio
    async def test_regular_session_uses_krx_and_skips_nxt(self, monkeypatch):
        from app.mcp_server.tooling import order_validation
        from app.mcp_server.tooling.market_session import DATA_STATE_FRESH

        ob = AsyncMock()
        monkeypatch.setattr(
            order_validation, "kr_market_data_state", lambda *a, **k: DATA_STATE_FRESH
        )
        monkeypatch.setattr(order_validation.market_data_service, "get_orderbook", ob)
        monkeypatch.setattr(
            order_validation,
            "_fetch_quote_equity_kr",
            AsyncMock(return_value={"price": 112400}),
        )

        price = await order_validation._get_current_price_for_order(
            "005930", "equity_kr"
        )
        assert price == pytest.approx(112400.0)
        ob.assert_not_awaited()  # NXT orderbook not consulted in regular session


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

        assert price == pytest.approx(50000000.0)
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
            order_type="limit",
            amount=5_000_000.0,
            price=100000.0,
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
            order_type="limit",
            amount=2_600_000.0,
            price=200.0,
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
            order_type="limit",
            amount=5_000_000.0,
            price=50000000.0,
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
                return {"odno": _unique_order_id("kr-high"), "ord_qty": quantity}

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
                return {"odno": _unique_order_id("us-high"), "success": True}

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
        assert buy_calls[0]["price"] == pytest.approx(250.0)

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
        mock.place_buy_order = AsyncMock(
            return_value={"uuid": _unique_order_id("crypto-high"), "side": "bid"}
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
            "place_buy_order",
            mock.place_buy_order,
        )
        monkeypatch.setattr(
            upbit_service,
            "adjust_price_to_upbit_unit",
            lambda price: price,
        )

        result = await tools["place_order"](
            symbol="KRW-BTC",
            side="buy",
            order_type="limit",
            amount=5_000_000.0,
            price=50000000.0,
            dry_run=False,
            thesis="Test thesis for BTC",
            strategy="test-strategy",
        )

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["preview"]["quantity"] == pytest.approx(0.1, rel=1e-6)
        mock.place_buy_order.assert_awaited_once_with(
            "KRW-BTC", 50000000.0, "0.10000000", "limit"
        )

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
            return_value={"uuid": _unique_order_id("crypto-limit"), "side": "bid"}
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
            order_type="limit",
            amount=5_000_000.0,
            price=50000000.0,
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
            return {
                "odno": _unique_order_id("kr-tick"),
                "ord_qty": quantity,
                "ord_unpr": price,
            }

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
            return {
                "odno": _unique_order_id("kr-tick-adjust"),
                "ord_qty": quantity,
                "ord_unpr": price,
            }

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
        order_type="limit",
        amount=1_000_000.0,
        price=80000.0,
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
    assert result["estimated_value"] == pytest.approx(700000.0)
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
            order_type="limit",
            amount=500_000.0,
            price=60000.0,
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
            return {"odno": _unique_order_id("kr-balance"), "ord_qty": quantity}

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


@pytest.mark.asyncio
async def test_place_order_kis_mock_sell_preview_uses_mock_holdings(monkeypatch):
    """kis_mock sell dry-run preview must not fall back to live KIS holdings."""
    tools = build_tools()
    kis_calls: list[bool] = []

    class MockKISClient:
        def __init__(self, is_mock: bool = False):
            self.is_mock = is_mock

        async def fetch_my_stocks(self, *, is_mock: bool = False):
            kis_calls.append(is_mock)
            avg_price = 227000.0 if is_mock else 194950.0
            return [
                {
                    "pdno": "005930",
                    "hldg_qty": "1",
                    "pchs_avg_pric": str(avg_price),
                }
            ]

    async def fetch_quote(symbol):
        assert symbol == "005930"
        return {"price": 226000.0}

    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="sell",
        order_type="limit",
        quantity=1,
        price=229500.0,
        dry_run=True,
        account_mode="kis_mock",
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["account_mode"] == "kis_mock"
    assert result["avg_buy_price"] == pytest.approx(227000.0)
    assert result["realized_pnl"] == pytest.approx(2500.0)
    assert kis_calls
    assert all(kis_calls)


@pytest.mark.asyncio
async def test_kis_mock_place_order_dry_run_warns_when_cash_lookup_unavailable(
    monkeypatch,
):
    """KIS mock dry-run preview should not fail solely because cash read is flaky."""
    tools = build_tools()

    class MockKISClient:
        def __init__(self, is_mock: bool = False):
            self.is_mock = is_mock

        async def inquire_domestic_cash_balance(self, *, is_mock: bool = False):
            assert self.is_mock is True
            assert is_mock is True
            raise TimeoutError()

    async def fetch_quote(symbol):
        assert symbol == "005930"
        return {"price": 318500.0}

    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    monkeypatch.setattr(orders_kis_variants, "validate_kis_mock_config", lambda: [])
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["kis_mock_place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=318500.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["account_mode"] == "kis_mock"
    assert result["estimated_value"] == pytest.approx(318500.0)
    assert "balance precheck unavailable" in result["warning"]
    assert "dry_run=True" in result["warning"]


@pytest.mark.asyncio
async def test_kis_mock_place_order_real_order_blocks_when_cash_lookup_unavailable(
    monkeypatch,
):
    """KIS mock confirmed submit remains fail-closed if cash cannot be verified."""
    tools = build_tools()
    order_calls: list[dict[str, object]] = []

    class MockKISClient:
        def __init__(self, is_mock: bool = False):
            self.is_mock = is_mock

        async def inquire_domestic_cash_balance(self, *, is_mock: bool = False):
            assert self.is_mock is True
            assert is_mock is True
            raise TimeoutError()

        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            order_calls.append(
                {
                    "stock_code": stock_code,
                    "order_type": order_type,
                    "quantity": quantity,
                    "price": price,
                }
            )
            return {"odno": "mock-should-not-submit"}

    async def fetch_quote(symbol):
        assert symbol == "005930"
        return {"price": 318500.0}

    monkeypatch.setattr(orders_registration, "validate_kis_mock_config", lambda: [])
    monkeypatch.setattr(orders_kis_variants, "validate_kis_mock_config", lambda: [])
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["kis_mock_place_order"](
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity=1,
        price=318500.0,
        dry_run=False,
    )

    assert result["success"] is False
    assert result["account_mode"] == "kis_mock"
    assert "balance precheck unavailable" in result["error"]
    assert "refusing to submit" in result["error"]
    assert order_calls == []


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
        order_type="limit",
        quantity=0.05,
        price=50000000.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert "orderable balance 0.03" in result["error"]
    assert "locked=0.02" in result["error"]


@pytest.mark.asyncio
async def test_place_order_crypto_market_sell_uses_orderable_only(monkeypatch):
    """MCP place_order must reject crypto market sell."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="market",
        dry_run=False,
    )
    _assert_market_rejected(result)


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

        def adjust_price_to_upbit_unit(self, price):
            return price

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
        order_type="limit",
        quantity=0.5,
        price=50000000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["quantity"] == pytest.approx(0.5)


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
        order_type="limit",
        amount=100000.0,
        price=50000000.0,
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

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 45000000.0}

        async def fetch_my_coins(self):
            return [
                {"currency": "BTC", "balance": "0.5", "avg_buy_price": "50000000.0"}
            ]

        def adjust_price_to_upbit_unit(self, price):
            return price

        async def place_sell_order(self, symbol, volume, price):
            return {
                "uuid": _unique_order_id("cd-sell-loss"),
                "side": "ask",
                "market": symbol,
                "volume": volume,
                "price": price,
            }

    _patch_runtime_attr(monkeypatch, "upbit_service", DummyUpbit())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=0.5,
        price=51000000.0,
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
        order_type="limit",
        quantity=0.5,
        price=51000000.0,
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

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols, use_cache=True):
            return {"KRW-BTC": 55000000.0}

        async def fetch_my_coins(self):
            return [
                {"currency": "BTC", "balance": "0.5", "avg_buy_price": "50000000.0"}
            ]

        def adjust_price_to_upbit_unit(self, price):
            return price

        async def place_sell_order(self, symbol, volume, price):
            return {
                "uuid": _unique_order_id("cd-sell-profit"),
                "side": "ask",
                "market": symbol,
                "volume": volume,
                "price": price,
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

    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=0.5,
        price=55000000.0,
        dry_run=False,
    )

    assert result["success"] is True
    assert len(fake_cooldown_service.recorded_symbols) == 0


@pytest.mark.asyncio
async def test_real_sell_is_accepted_only_no_journal_close_at_send(monkeypatch) -> None:
    """ROB-407: live crypto sell records accepted-only and must NOT close journals
    at send. Journal close happens later from confirmed fill evidence in
    live_reconcile_orders (see tests/mcp_server/tooling/test_live_order_ledger.py)."""
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
        "place_sell_order",
        AsyncMock(
            return_value={
                "uuid": _unique_order_id("rc-sell-close"),
                "side": "ask",
                "market": "KRW-BTC",
                "price": "95000000",
                "volume": "0.01",
            }
        ),
    )
    save_mock = AsyncMock(return_value=123)
    monkeypatch.setattr(order_execution, "_save_order_fill", save_mock)
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())

    close_mock = AsyncMock()
    monkeypatch.setattr(order_execution, "_close_journals_on_sell", close_mock)

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=0.01,
        price=95000000.0,
        dry_run=False,
        reason="rebalance",
        exit_reason="take_profit",
    )

    assert result["success"] is True
    assert result["fill_recorded"] is False
    assert result["broker_status"] == "accepted"
    assert "journals_closed" not in result
    close_mock.assert_not_awaited()  # no journal close at send
    save_mock.assert_not_awaited()  # no fill booked at send


@pytest.mark.asyncio
async def test_sell_send_does_not_touch_journals_even_if_close_would_fail(
    monkeypatch,
) -> None:
    """ROB-407: send path never calls _close_journals_on_sell, so a would-be close
    failure cannot affect the accepted-only sell. Close is reconcile-time only."""
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
        "place_sell_order",
        AsyncMock(
            return_value={
                "uuid": _unique_order_id("rc-sell-closefail"),
                "side": "ask",
                "market": "KRW-BTC",
                "price": "95000000",
                "volume": "0.01",
            }
        ),
    )
    monkeypatch.setattr(
        order_execution, "_save_order_fill", AsyncMock(return_value=123)
    )
    monkeypatch.setattr(order_execution, "_link_journal_to_fill", AsyncMock())
    close_mock = AsyncMock(side_effect=RuntimeError("db timeout"))
    monkeypatch.setattr(order_execution, "_close_journals_on_sell", close_mock)

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="sell",
        order_type="limit",
        quantity=0.01,
        price=95000000.0,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["fill_recorded"] is False
    assert "journal_warning" not in result
    close_mock.assert_not_awaited()  # close is never attempted at send


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
        order_type="limit",
        quantity=0.01,
        price=95000000.0,
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
    async def test_real_buy_is_accepted_only_no_fill_at_send(self, monkeypatch) -> None:
        """ROB-407: live crypto buy records accepted-only; the fill is NOT saved to
        review.trades at send. _save_order_fill is invoked only by
        live_reconcile_orders once broker fill evidence confirms execution."""
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
                    "uuid": _unique_order_id("test-fill"),
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
        assert result.get("fill_recorded") is False
        assert result["broker_status"] == "accepted"
        save_mock.assert_not_awaited()  # no fill booked at send (reconcile-gated)

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
    async def test_real_buy_no_journal_at_send(self, monkeypatch) -> None:
        """ROB-407: live crypto buy is accepted-only; no trade journal is created at
        send. Journal creation + fill link happen in live_reconcile_orders once the
        broker confirms the fill."""
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
                    "uuid": _unique_order_id("rc-buy-journal"),
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
        assert result["journal_created"] is False
        assert result["broker_status"] == "accepted"
        create_journal_mock.assert_not_awaited()  # no journal at send

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
                    "uuid": _unique_order_id("ofr-sell"),
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
    async def test_buy_send_does_not_create_journal_even_if_it_would_fail(
        self, monkeypatch
    ) -> None:
        """ROB-407: send path never calls _create_trade_journal_for_buy, so a
        would-be journal failure cannot affect the accepted-only buy. Journal
        creation is reconcile-time only."""
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
                    "uuid": _unique_order_id("ofr-buy-jfail"),
                    "side": "bid",
                    "market": "KRW-BTC",
                    "price": "95000000",
                    "volume": "0.001",
                }
            ),
        )

        create_journal_mock = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(
            order_execution,
            "_create_trade_journal_for_buy",
            create_journal_mock,
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
        assert "journal_warning" not in result
        create_journal_mock.assert_not_awaited()  # not called at send


# ----------------------------------------------------------------------
# Market order rejection tests (policy change)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_paper_market_rejected():
    """MCP place_order must reject market orders even in paper mode."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=1_000_000,
        dry_run=False,
        account_type="paper",
        thesis="t",
        strategy="s",
    )
    _assert_market_rejected(result)


@pytest.mark.asyncio
async def test_place_order_paper_market_sell_rejected():
    """MCP place_order must reject market sell orders in paper mode."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="005930",
        side="sell",
        order_type="market",
        dry_run=False,
        account_type="paper",
    )
    _assert_market_rejected(result)


@pytest.mark.asyncio
async def test_place_order_limit_still_works_after_market_block(monkeypatch):
    """Policy change must not regress the limit path."""
    tools = build_tools()
    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="limit",
        price=50_000_000,
        quantity=0.01,
        dry_run=True,
        thesis="t",
        strategy="s",
    )
    # limit dry-run should succeed (or at least not be the market-rejection error)
    assert result.get("error") != EXPECTED_MARKET_ERROR


# ----------------------------------------------------------------------
# ROB-477: sell limit above market fill-risk warning (per-order)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_sell_limit_above_market_warns(monkeypatch):
    """Limit sell above current price returns informational fill-risk details."""
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"quantity": 8.0, "avg_price": 40.0}),
    )
    result = await order_validation._preview_sell(
        symbol="IONQ",
        order_type="limit",
        quantity=2.0,
        price=64.0,
        current_price=63.95,
        market_type="equity_us",
    )
    assert "error" not in result
    assert "sell_limit_above_market" in result.get("warnings", [])
    assert result["fill_distance"]["distance_usd"] == pytest.approx(0.05)
    assert result["fill_distance"]["distance_pct"] == pytest.approx(0.0782, abs=1e-4)


@pytest.mark.asyncio
async def test_preview_sell_limit_at_market_no_warning(monkeypatch):
    monkeypatch.setattr(
        order_validation,
        "_get_holdings_for_order",
        AsyncMock(return_value={"quantity": 8.0, "avg_price": 40.0}),
    )
    result = await order_validation._preview_sell(
        symbol="IONQ",
        order_type="limit",
        quantity=2.0,
        price=63.95,
        current_price=63.95,
        market_type="equity_us",
    )
    assert "error" not in result
    assert "sell_limit_above_market" not in result.get("warnings", [])
    assert "fill_distance" not in result


# ----------------------------------------------------------------------
# ROB-477: sell_ladder_fill_preview read-only tool
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_all_above_market():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[
            {"limit_price": 66.0, "quantity": 2.0},
            {"limit_price": 68.0, "quantity": 3.0},
        ],
    )
    assert result["success"] is True
    assert result["read_only"] is True
    assert "ladder_all_above_market" in result["warnings"]
    assert "ladder_missing_near_market_anchor" in result["warnings"]
    assert result["fill_safety"]["suggestedAnchorRung"]["limitPriceUsd"] == 63.95


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_near_anchor_only_all_above():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[
            {"limit_price": 64.0, "quantity": 2.0},
            {"limit_price": 68.0, "quantity": 3.0},
        ],
    )
    assert "ladder_all_above_market" in result["warnings"]
    assert "ladder_missing_near_market_anchor" not in result["warnings"]


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_rejects_bad_payload():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=63.95,
        rungs=[{"price_typo": 64.0}],
    )
    assert result["success"] is False
    assert "limit_price" in result["error"] or "invalid" in result["error"]


@pytest.mark.asyncio
async def test_sell_ladder_fill_preview_rejects_non_positive_anchor():
    tools = build_tools()
    result = await tools["sell_ladder_fill_preview"](
        symbol="IONQ",
        anchor_price=0.0,
        rungs=[{"limit_price": 64.0}],
    )
    assert result["success"] is False
