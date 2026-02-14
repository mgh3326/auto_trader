import logging
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pandas as pd
import pytest
import yfinance as yf

from app.core.config import settings
from app.mcp_server.tooling import (
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_handlers,
    fundamentals_sources_binance,
    fundamentals_sources_coingecko,
    fundamentals_sources_finnhub,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    market_data_indicators,
    market_data_quotes,
    order_execution,
    orders_history,
    orders_modify_cancel,
    portfolio_cash,
    portfolio_holdings,
    shared,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.models.dca_plan import DcaPlan, DcaPlanStatus, DcaPlanStep, DcaStepStatus
from app.services import naver_finance
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.dca_service import DcaService

# from app.mcp_server.tick_size import adjust_tick_size_kr  # TODO: Remove if not needed


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    """Async context manager wrapper for an AsyncSession-like object."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


_PATCH_MODULES = (
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_handlers,
    fundamentals_sources_binance,
    fundamentals_sources_coingecko,
    fundamentals_sources_finnhub,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    market_data_quotes,
    order_execution,
    orders_history,
    orders_modify_cancel,
    portfolio_cash,
    portfolio_holdings,
)


def _patch_runtime_attr(
    monkeypatch: pytest.MonkeyPatch, attr_name: str, value: object
) -> None:
    matched = False
    for module in _PATCH_MODULES:
        if hasattr(module, attr_name):
            monkeypatch.setattr(module, attr_name, value)
            matched = True
    if not matched:
        raise AttributeError(f"No runtime module exposes attribute '{attr_name}'")


def _patch_httpx_async_client(
    monkeypatch: pytest.MonkeyPatch, async_client_class: type
) -> None:
    for module in (
        analysis_tool_handlers,
        fundamentals_sources_binance,
        fundamentals_sources_coingecko,
        fundamentals_sources_indices,
        fundamentals_sources_naver,
    ):
        monkeypatch.setattr(module.httpx, "AsyncClient", async_client_class)


def _patch_yf_ticker(monkeypatch: pytest.MonkeyPatch, ticker_factory: object) -> None:
    monkeypatch.setattr(fundamentals_sources_naver.yf, "Ticker", ticker_factory)
    monkeypatch.setattr(fundamentals_sources_indices.yf, "Ticker", ticker_factory)


def _single_row_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "time": "09:30:00",
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
            }
        ]
    )


@pytest.mark.asyncio
async def test_get_cash_balance_all_accounts(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_krw_balance(self):
            return 500000.0

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_balance",
        MockUpbitService().fetch_krw_balance,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"]()

    assert len(result["accounts"]) == 3
    assert result["summary"]["total_krw"] == 1500000.0
    assert result["summary"]["total_usd"] == 500.0
    assert len(result["errors"]) == 0

    upbit_account = next(acc for acc in result["accounts"] if acc["account"] == "upbit")
    assert upbit_account["balance"] == 500000.0
    assert upbit_account["formatted"] == "500,000 KRW"

    kis_overseas_account = next(
        acc for acc in result["accounts"] if acc["account"] == "kis_overseas"
    )
    assert kis_overseas_account["balance"] == 500.0
    assert kis_overseas_account["orderable"] == 450.0
    assert kis_overseas_account["exchange_rate"] is None


@pytest.mark.asyncio
async def test_get_cash_balance_with_account_filter(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_balance",
        AsyncMock(side_effect=RuntimeError("Upbit API error")),
    )

    result = await tools["get_cash_balance"](account="upbit")
    assert len(result["accounts"]) == 0
    assert result["summary"]["total_krw"] == 0.0

    result = await tools["get_cash_balance"](account="kis")
    assert len(result["accounts"]) == 2
    assert result["accounts"][0]["account"] == "kis_domestic"
    assert result["accounts"][1]["account"] == "kis_overseas"


@pytest.mark.asyncio
async def test_get_cash_balance_partial_failure(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_krw_balance(self):
            raise RuntimeError("Upbit API error")

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_gnrl_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        upbit_service,
        "fetch_krw_balance",
        MockUpbitService().fetch_krw_balance,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"]()

    assert len(result["accounts"]) == 2  # KIS domestic + overseas succeeded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["source"] == "upbit"

    kis_overseas_account = next(
        acc for acc in result["accounts"] if acc["account"] == "kis_overseas"
    )
    assert kis_overseas_account["balance"] == 500.0
    assert kis_overseas_account["orderable"] == 450.0
    assert kis_overseas_account["exchange_rate"] is None


@pytest.mark.asyncio
async def test_get_cash_balance_kis_domestic_fail_close(monkeypatch):
    tools = build_tools()

    class FailingKISClient:
        async def inquire_domestic_cash_balance(self):
            raise RuntimeError("domestic balance failed")

    _patch_runtime_attr(monkeypatch, "KISClient", FailingKISClient)

    with pytest.raises(RuntimeError, match="KIS domestic cash balance query failed"):
        await tools["get_cash_balance"](account="kis_domestic")


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_fail_close(monkeypatch):
    tools = build_tools()

    class FailingKISClient:
        async def inquire_overseas_margin(self):
            raise RuntimeError("overseas margin failed")

    _patch_runtime_attr(monkeypatch, "KISClient", FailingKISClient)

    with pytest.raises(RuntimeError, match="KIS overseas cash balance query failed"):
        await tools["get_cash_balance"](account="kis_overseas")


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

        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "5000000",
                "stck_cash_ord_psbl_amt": "5000000",
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


@pytest.mark.asyncio
async def test_get_order_history_pending_crypto(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return [
                {
                    "uuid": "uuid-1",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "50000000.0",
                    "volume": "0.001",
                    "remaining_volume": "0.001",
                    "executed_volume": "0.0",
                    "market": "KRW-BTC",
                    "created_at": "2024-01-01T00:00:00Z",
                    "state": "wait",
                }
            ]

    class MockKISClient:
        async def inquire_korea_orders(self):
            return []

        async def inquire_overseas_orders(self, exchange_code):
            return []

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_order_history"](status="pending")

    assert result["total_available"] == 1
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["order_id"] == "uuid-1"
    assert order["symbol"] == "KRW-BTC"
    assert order["side"] == "buy"
    assert order["status"] == "pending"
    assert order["remaining_qty"] == 0.001


@pytest.mark.asyncio
async def test_get_order_history_pending_kr_equity(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_korea_orders(self):
            return [
                {
                    "ord_no": "12345",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "005930",
                    "ord_qty": "10",
                    "ccld_qty": "0",
                    "ord_unpr": "80000",
                    "ccld_unpr": "0",
                    "ord_dt": "20240101",
                    "ord_tmd": "093000",
                    "prcs_stat_name": "접수",
                }
            ]

        async def inquire_overseas_orders(self, exchange_code):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_order_history"](status="pending", market="kr")

    assert result["market"] == "kr"
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["order_id"] == "12345"
    assert order["symbol"] == "005930"
    assert order["side"] == "buy"
    assert order["status"] == "pending"
    assert order["ordered_qty"] == 10


@pytest.mark.asyncio
async def test_get_order_history_pending_us_equity(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_korea_orders(self):
            return []

        async def inquire_overseas_orders(self, exchange_code):
            if exchange_code == "NASD":
                return [
                    {
                        "odno": "67890",
                        "sll_buy_dvsn_cd": "02",
                        "pdno": "AAPL",
                        "ft_ord_qty": "100",
                        "ft_ccld_qty": "50",
                        "ft_ord_unpr3": "200.0",
                        "ft_ccld_unpr3": "199.5",
                        "ord_dt": "20240101",
                        "ord_tmd": "093000",
                        "prcs_stat_name": "접수",
                    }
                ]
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_order_history"](status="pending", market="us")

    assert result["market"] == "us"
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["order_id"] == "67890"
    assert order["symbol"] == "AAPL"
    assert order["remaining_qty"] == 50


@pytest.mark.asyncio
async def test_get_order_history_pending_with_symbol_filter(monkeypatch):
    tools = build_tools()

    mock_fetch_open_orders = AsyncMock(
        return_value=[
            {
                "uuid": "uuid-1",
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000.0",
                "volume": "0.001",
                "remaining_volume": "0.001",
                "executed_volume": "0.0",
                "market": "KRW-BTC",
                "created_at": "2024-01-01",
                "state": "wait",
            }
        ]
    )

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        mock_fetch_open_orders,
    )

    result = await tools["get_order_history"](symbol="KRW-BTC", status="pending")

    mock_fetch_open_orders.assert_awaited_once_with(market="KRW-BTC")
    assert len(result["orders"]) == 1
    assert result["orders"][0]["symbol"] == "KRW-BTC"


@pytest.mark.asyncio
async def test_get_order_history_pending_empty_result(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return []

    class MockKISClient:
        async def inquire_korea_orders(self):
            return []

        async def inquire_overseas_orders(self, exchange_code):
            return []

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_order_history"](status="pending")

    assert result["total_available"] == 0
    assert len(result["orders"]) == 0


@pytest.mark.asyncio
async def test_get_order_history_pending_partial_failure(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            raise RuntimeError("Upbit API error")

    class MockKISClient:
        async def inquire_korea_orders(self):
            return [{"ord_no": "12345", "sll_buy_dvsn_cd": "02", "pdno": "005930"}]

        async def inquire_overseas_orders(self, exchange_code="NASD"):
            return []

    monkeypatch.setattr(
        upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )
    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_order_history"](status="pending")

    assert len(result["errors"]) == 1
    assert result["errors"][0]["market"] == "crypto"
    assert len(result["orders"]) == 1


@pytest.mark.asyncio
async def test_cancel_order_upbit_uuid(monkeypatch):
    tools = build_tools()
    test_uuid = "550e8400-e29b-41d4-a716-446655440000"

    class MockUpbitService:
        async def cancel_orders(self, order_uuids):
            return [{"uuid": test_uuid, "created_at": "2024-01-01T00:00:00Z"}]

    monkeypatch.setattr(
        upbit_service, "cancel_orders", MockUpbitService().cancel_orders
    )

    result = await tools["cancel_order"](order_id=test_uuid)

    assert result["success"] is True
    assert result["order_id"] == test_uuid


@pytest.mark.asyncio
async def test_cancel_order_kis_domestic_auto_lookup(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_korea_orders(self):
            return [
                {
                    "ord_no": "12345",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "005930",
                    "ord_qty": "10",
                    "ord_unpr": "80000",
                    "ord_tmd": "2024-01-01",
                }
            ]

        async def cancel_korea_order(
            self, order_number, stock_code, quantity, price, order_type
        ):
            return {"ord_no": order_number, "ord_tmd": "2024-01-01 10:00:00"}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["cancel_order"](order_id="12345", symbol="005930", market="kr")

    assert result["success"] is True
    assert result["symbol"] == "005930"


@pytest.mark.asyncio
async def test_cancel_order_kis_overseas(monkeypatch):
    tools = build_tools()

    class MockKISClient:
        async def inquire_overseas_orders(self, exchange_code):
            return [
                {
                    "odno": "67890",
                    "sll_buy_dvsn_cd": "02",
                    "pdno": "AAPL",
                    "ft_ord_qty": "100",
                    "ft_ord_unpr3": "200.0",
                    "nccs_qty": "50",
                    "ord_tmd": "2024-01-01",
                }
            ]

        async def cancel_overseas_order(
            self, order_number, symbol, exchange_code, quantity
        ):
            return {"odno": order_number, "ord_tmd": "2024-01-01 10:00:00"}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["cancel_order"](order_id="67890", symbol="AAPL", market="us")

    assert result["success"] is True
    assert result["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_cancel_order_uuid_auto_detect_market(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def cancel_orders(self, order_uuids):
            return [
                {
                    "uuid": "550e8400-e29b-41d4-a716-446655440123",
                    "created_at": "2024-01-01",
                }
            ]

    monkeypatch.setattr(
        upbit_service, "cancel_orders", MockUpbitService().cancel_orders
    )

    uuid = "550e8400-e29b-41d4-a716-446655440123"
    result = await tools["cancel_order"](order_id=uuid)

    assert result["success"] is True
    assert result["order_id"] == uuid


@pytest.mark.asyncio
async def test_search_symbol_empty_query_returns_empty():
    tools = build_tools()

    result = await tools["search_symbol"]("   ")

    assert result == []


@pytest.mark.asyncio
async def test_search_symbol_clamps_limit_and_shapes(monkeypatch):
    tools = build_tools()

    # Mock master data
    _patch_runtime_attr(
        monkeypatch,
        "get_kospi_name_to_code",
        lambda: {"삼성전자": "005930", "삼성SDI": "006400"},
    )
    _patch_runtime_attr(monkeypatch, "get_kosdaq_name_to_code", lambda: {})
    _patch_runtime_attr(
        monkeypatch,
        "get_us_stocks_data",
        lambda: {
            "symbol_to_exchange": {},
            "symbol_to_name_kr": {},
            "symbol_to_name_en": {},
        },
    )

    result = await tools["search_symbol"]("삼성", limit=500)

    # limit should be capped at 100
    assert len(result) == 2
    assert result[0]["symbol"] == "005930"
    assert result[0]["name"] == "삼성전자"
    assert result[0]["instrument_type"] == "equity_kr"
    assert result[0]["exchange"] == "KOSPI"


@pytest.mark.asyncio
async def test_search_symbol_with_market_filter(monkeypatch):
    tools = build_tools()

    # Mock master data
    _patch_runtime_attr(
        monkeypatch,
        "get_kospi_name_to_code",
        lambda: {"애플": "123456"},
    )
    _patch_runtime_attr(monkeypatch, "get_kosdaq_name_to_code", lambda: {})
    _patch_runtime_attr(
        monkeypatch,
        "get_us_stocks_data",
        lambda: {
            "symbol_to_exchange": {"AAPL": "NASDAQ"},
            "symbol_to_name_kr": {"AAPL": "애플"},
            "symbol_to_name_en": {"AAPL": "Apple Inc."},
        },
    )

    # Search with us market filter
    result = await tools["search_symbol"]("애플", market="us")

    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_search_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()

    def raise_error():
        raise RuntimeError("master data failed")

    _patch_runtime_attr(monkeypatch, "get_kospi_name_to_code", raise_error)

    result = await tools["search_symbol"]("samsung")

    assert len(result) == 1
    assert result[0]["error"] == "master data failed"
    assert result[0]["source"] == "master"
    assert result[0]["query"] == "samsung"


@pytest.mark.asyncio
async def test_get_quote_crypto(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(return_value={"KRW-BTC": 123.4})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

    result = await tools["get_quote"]("krw-btc")

    mock_fetch.assert_awaited_once_with(["KRW-BTC"])
    assert result == {
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "price": 123.4,
        "source": "upbit",
    }


@pytest.mark.asyncio
async def test_get_quote_crypto_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("upbit down"))
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

    result = await tools["get_quote"]("KRW-BTC")

    assert result == {
        "error": "upbit down",
        "source": "upbit",
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
    }


@pytest.mark.asyncio
async def test_get_quote_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0  # price = close
    assert result["open"] == 100.0


@pytest.mark.asyncio
async def test_get_quote_korean_equity_returns_error_payload(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            raise RuntimeError("kis down")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result == {
        "error": "kis down",
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_quote_korean_etf(monkeypatch):
    """Test get_quote with Korean ETF code (alphanumeric like 0123G0)."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0123G0")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0


@pytest.mark.asyncio
async def test_get_quote_korean_etf_with_explicit_market(monkeypatch):
    """Test get_quote with Korean ETF code and explicit market=kr."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0117V0", market="kr")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    tools = build_tools()

    # Mock yfinance Ticker
    class MockFastInfo:
        last_price = 205.0
        regular_market_previous_close = 200.0
        open = 201.0
        day_high = 210.0
        day_low = 199.0
        last_volume = 50000000

    class MockTicker:
        fast_info = MockFastInfo()

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: MockTicker())

    result = await tools["get_quote"]("AAPL")

    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["price"] == 205.0
    assert result["previous_close"] == 200.0
    assert result["open"] == 201.0
    assert result["high"] == 210.0
    assert result["low"] == 199.0
    assert result["volume"] == 50000000


@pytest.mark.asyncio
async def test_get_quote_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()

    def raise_error(symbol):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr("yfinance.Ticker", raise_error)

    result = await tools["get_quote"]("AAPL")

    assert result == {
        "error": "yahoo down",
        "source": "yahoo",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


@pytest.mark.asyncio
async def test_get_quote_raises_on_invalid_symbol():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_quote"]("")

    # Note: Numeric symbols like "1234" are now normalized to "001234" for KR market,
    # so we test with a clearly invalid format instead
    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("!@#$")


@pytest.mark.asyncio
async def test_get_quote_market_crypto_requires_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="crypto symbols must include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("BTC", market="crypto")


@pytest.mark.asyncio
async def test_get_quote_market_kr_requires_digits():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_quote"]("AAPL", market="kr")


@pytest.mark.asyncio
async def test_get_quote_market_us_rejects_crypto_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="us equity symbols must not include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("KRW-BTC", market="us")


@pytest.mark.asyncio
async def test_get_ohlcv_crypto(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=300)

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="day", end_date=None
    )
    assert result["instrument_type"] == "crypto"
    assert result["source"] == "upbit"
    assert result["count"] == 200
    assert result["period"] == "day"
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_with_period_week(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=52, period="week")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=52, period="week", end_date=None
    )
    assert result["period"] == "week"


@pytest.mark.asyncio
async def test_get_ohlcv_with_end_date(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    await tools["get_ohlcv"]("KRW-BTC", count=100, end_date="2024-06-30")

    # Verify end_date was parsed and passed
    call_args = mock_fetch.call_args
    assert call_args.kwargs["end_date"].year == 2024
    assert call_args.kwargs["end_date"].month == 6
    assert call_args.kwargs["end_date"].day == 30


@pytest.mark.asyncio
async def test_get_ohlcv_serializes_timestamps(monkeypatch):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01"),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10,
                "value": float("nan"),
            }
        ]
    )
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1)

    row = result["rows"][0]
    assert isinstance(row["date"], str)
    assert "2024-01-01" in row["date"]
    assert row["value"] is None


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", count=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["count"] == 10
    assert result["period"] == "day"
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity_with_period_month(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            called["period"] = period
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", count=24, period="month")

    assert called["period"] == "M"  # KIS uses M for month
    assert result["period"] == "month"


@pytest.mark.asyncio
async def test_get_ohlcv_korean_etf(monkeypatch):
    """Test get_ohlcv with Korean ETF code (alphanumeric like 0123G0)."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0123G0", count=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["count"] == 10


@pytest.mark.asyncio
async def test_get_ohlcv_korean_etf_with_explicit_market(monkeypatch):
    """Test get_ohlcv with Korean ETF code and explicit market=kr."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0117V0", market="kr", count=5)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("yahoo timeout"))
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    assert result == {
        "error": "yahoo timeout",
        "source": "yahoo",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    mock_fetch.assert_awaited_once_with(
        ticker="AAPL", days=5, period="day", end_date=None
    )
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["count"] == 5
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_input():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_ohlcv"]("")

    with pytest.raises(ValueError, match="count must be > 0"):
        await tools["get_ohlcv"]("AAPL", count=0)

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_ohlcv"]("1234")


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_period():
    tools = build_tools()

    with pytest.raises(ValueError, match="period must be 'day', 'week', or 'month'"):
        await tools["get_ohlcv"]("AAPL", period="hour")


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_end_date():
    tools = build_tools()

    with pytest.raises(ValueError, match="end_date must be ISO format"):
        await tools["get_ohlcv"]("AAPL", end_date="invalid-date")


@pytest.mark.asyncio
async def test_get_ohlcv_market_kr_requires_digits():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_ohlcv"]("AAPL", market="kr")


@pytest.mark.asyncio
async def test_get_ohlcv_market_us_rejects_crypto_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="us equity symbols must not include KRW-/USDT- prefix"
    ):
        await tools["get_ohlcv"]("KRW-BTC", market="us")


@pytest.mark.asyncio
async def test_get_indicators_supports_new_indicators(monkeypatch):
    tools = build_tools()
    rows = 80
    close = pd.Series([100.0 + i * 0.2 + np.sin(i) for i in range(rows)])
    df = pd.DataFrame(
        {
            "close": close,
            "high": close + 1.5,
            "low": close - 1.5,
            "volume": pd.Series([1000.0 + i * 10 for i in range(rows)]),
        }
    )

    _patch_runtime_attr(
        monkeypatch, "_fetch_ohlcv_for_indicators", AsyncMock(return_value=df)
    )

    result = await tools["get_indicators"](
        "KRW-BTC", indicators=["adx", "stoch_rsi", "obv"]
    )

    assert "error" not in result
    assert "indicators" in result
    assert "adx" in result["indicators"]
    assert "stoch_rsi" in result["indicators"]
    assert "obv" in result["indicators"]
    assert set(result["indicators"]["adx"].keys()) == {"adx", "plus_di", "minus_di"}
    assert set(result["indicators"]["stoch_rsi"].keys()) == {"k", "d"}
    assert set(result["indicators"]["obv"].keys()) == {"obv", "signal", "divergence"}


@pytest.mark.asyncio
async def test_get_indicators_rejects_invalid_indicator_with_new_valid_options():
    tools = build_tools()

    with pytest.raises(ValueError, match="Invalid indicator") as exc_info:
        await tools["get_indicators"]("KRW-BTC", indicators=["not_a_real_indicator"])

    message = str(exc_info.value)
    assert "Valid options" in message
    assert "adx" in message
    assert "stoch_rsi" in message
    assert "obv" in message


@pytest.mark.asyncio
async def test_get_indicators_obv_returns_error_when_volume_column_missing(monkeypatch):
    tools = build_tools()
    rows = 40
    close = pd.Series([100.0 + i * 0.1 for i in range(rows)])
    df_no_volume = pd.DataFrame(
        {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
        }
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=df_no_volume),
    )

    result = await tools["get_indicators"]("AAPL", indicators=["obv"])

    assert result["source"] == "yahoo"
    assert "error" in result
    assert "Missing required columns" in result["error"]
    assert "volume" in result["error"]


@pytest.mark.unit
def test_calculate_volume_profile_distributes_volume_proportionally():
    df = pd.DataFrame(
        [
            {
                "low": 0.0,
                "high": 10.0,
                "volume": 100.0,
            }
        ]
    )

    result = market_data_indicators._calculate_volume_profile(
        df, bins=2, value_area_ratio=0.70
    )

    assert result["price_range"] == {"low": 0, "high": 10}
    assert result["poc"]["volume"] == 50
    assert result["profile"][0]["volume"] == 50
    assert result["profile"][1]["volume"] == 50
    assert result["profile"][0]["volume_pct"] == 50
    assert result["profile"][1]["volume_pct"] == 50


@pytest.mark.unit
class TestNormalizeMarket:
    """Tests for _normalize_market helper function."""

    def test_returns_none_for_empty(self):
        assert shared.normalize_market(None) is None
        assert shared.normalize_market("") is None
        assert shared.normalize_market("   ") is None

    def test_crypto_aliases(self):
        for alias in ["crypto", "upbit", "krw", "usdt"]:
            assert shared.normalize_market(alias) == "crypto"

    def test_equity_kr_aliases(self):
        for alias in ["kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr"]:
            assert shared.normalize_market(alias) == "equity_kr"

    def test_equity_us_aliases(self):
        for alias in ["us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"]:
            assert shared.normalize_market(alias) == "equity_us"

    def test_case_insensitive(self):
        assert shared.normalize_market("CRYPTO") == "crypto"
        assert shared.normalize_market("KR") == "equity_kr"
        assert shared.normalize_market("Us") == "equity_us"

    def test_unknown_returns_none(self):
        assert shared.normalize_market("unknown") is None
        assert shared.normalize_market("invalid") is None


@pytest.mark.unit
class TestSymbolDetection:
    """Tests for symbol detection helper functions."""

    def test_is_korean_equity_code(self):
        # Regular stocks (6 digits)
        assert shared.is_korean_equity_code("005930") is True
        assert shared.is_korean_equity_code("000660") is True
        assert shared.is_korean_equity_code("  005930  ") is True
        # ETF/ETN (6 alphanumeric)
        assert shared.is_korean_equity_code("0123G0") is True  # ETF
        assert shared.is_korean_equity_code("0117V0") is True  # ETF
        assert shared.is_korean_equity_code("12345A") is True  # alphanumeric
        assert shared.is_korean_equity_code("0123g0") is True  # lowercase
        # Invalid codes
        assert shared.is_korean_equity_code("00593") is False  # 5 chars
        assert shared.is_korean_equity_code("0059300") is False  # 7 chars
        assert shared.is_korean_equity_code("AAPL") is False  # 4 chars
        assert shared.is_korean_equity_code("0123-0") is False  # contains hyphen

    def test_is_crypto_market(self):
        assert shared.is_crypto_market("KRW-BTC") is True
        assert shared.is_crypto_market("krw-btc") is True
        assert shared.is_crypto_market("USDT-BTC") is True
        assert shared.is_crypto_market("usdt-eth") is True
        assert shared.is_crypto_market("BTC") is False
        assert shared.is_crypto_market("AAPL") is False
        assert shared.is_crypto_market("005930") is False

    def test_is_us_equity_symbol(self):
        assert shared.is_us_equity_symbol("AAPL") is True
        assert shared.is_us_equity_symbol("MSFT") is True
        assert shared.is_us_equity_symbol("BRK.B") is True
        assert shared.is_us_equity_symbol("KRW-BTC") is False  # crypto prefix
        assert shared.is_us_equity_symbol("005930") is False  # all digits


@pytest.mark.unit
class TestNormalizeValue:
    """Tests for _normalize_value helper function."""

    def test_none_returns_none(self):
        assert shared.normalize_value(None) is None

    def test_nan_returns_none(self):
        import numpy as np

        assert shared.normalize_value(float("nan")) is None
        assert shared.normalize_value(np.nan) is None

    def test_datetime_returns_isoformat(self):
        import datetime

        dt = datetime.datetime(2024, 1, 15, 10, 30, 0)
        assert shared.normalize_value(dt) == "2024-01-15T10:30:00"

        d = datetime.date(2024, 1, 15)
        assert shared.normalize_value(d) == "2024-01-15"

    def test_timedelta_returns_seconds(self):
        td = pd.Timedelta(hours=1, minutes=30)
        assert shared.normalize_value(td) == 5400.0

    def test_numpy_scalar_returns_python_type(self):
        import numpy as np

        assert shared.normalize_value(np.int64(42)) == 42
        assert shared.normalize_value(np.float64(3.14)) == 3.14

    def test_regular_values_pass_through(self):
        assert shared.normalize_value(42) == 42
        assert shared.normalize_value(3.14) == 3.14
        assert shared.normalize_value("hello") == "hello"


@pytest.mark.unit
class TestResolveMarketType:
    """Tests for _resolve_market_type helper function."""

    def test_explicit_crypto_normalizes_symbol(self):
        market_type, symbol = shared.resolve_market_type("krw-btc", "crypto")
        assert market_type == "crypto"
        assert symbol == "KRW-BTC"

    def test_explicit_crypto_rejects_invalid_prefix(self):
        with pytest.raises(ValueError, match="KRW-/USDT- prefix"):
            shared.resolve_market_type("BTC", "crypto")

    def test_explicit_equity_kr_validates_digits(self):
        market_type, symbol = shared.resolve_market_type("005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_explicit_equity_kr_validates_etf(self):
        """Test explicit market=kr with ETF alphanumeric code."""
        market_type, symbol = shared.resolve_market_type("0123G0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_explicit_equity_kr_validates_etf_lowercase(self):
        """Test explicit market=kr with lowercase ETF code (should be accepted)."""
        market_type, symbol = shared.resolve_market_type("0123g0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123g0"

    def test_explicit_equity_kr_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="6 alphanumeric"):
            shared.resolve_market_type("AAPL", "kr")

    def test_explicit_equity_us_rejects_crypto_prefix(self):
        with pytest.raises(ValueError, match="must not include KRW-/USDT-"):
            shared.resolve_market_type("KRW-BTC", "us")

    def test_auto_detect_crypto(self):
        market_type, symbol = shared.resolve_market_type("krw-eth", None)
        assert market_type == "crypto"
        assert symbol == "KRW-ETH"

    def test_auto_detect_korean_equity(self):
        market_type, symbol = shared.resolve_market_type("005930", None)
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_auto_detect_korean_etf(self):
        """Test auto-detection of Korean ETF code (alphanumeric)."""
        market_type, symbol = shared.resolve_market_type("0123G0", None)
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_auto_detect_korean_etf_another(self):
        """Test auto-detection with another ETF code pattern."""
        market_type, symbol = shared.resolve_market_type("0117V0", None)
        assert market_type == "equity_kr"
        assert symbol == "0117V0"

    def test_auto_detect_us_equity(self):
        market_type, symbol = shared.resolve_market_type("AAPL", None)
        assert market_type == "equity_us"
        assert symbol == "AAPL"

    def test_unsupported_symbol_raises(self):
        with pytest.raises(ValueError, match="Unsupported symbol format"):
            shared.resolve_market_type("1234", None)

    def test_market_aliases(self):
        # Test various market aliases
        assert shared.resolve_market_type("KRW-BTC", "upbit")[0] == "crypto"
        assert shared.resolve_market_type("005930", "kospi")[0] == "equity_kr"
        assert shared.resolve_market_type("AAPL", "nasdaq")[0] == "equity_us"


@pytest.mark.unit
class TestErrorPayload:
    """Tests for _error_payload helper function."""

    def test_minimal_payload(self):
        result = shared.error_payload(source="test", message="error occurred")
        assert result == {"error": "error occurred", "source": "test"}

    def test_with_symbol(self):
        result = shared.error_payload(
            source="upbit", message="not found", symbol="KRW-BTC"
        )
        assert result == {
            "error": "not found",
            "source": "upbit",
            "symbol": "KRW-BTC",
        }

    def test_with_all_fields(self):
        result = shared.error_payload(
            source="yahoo",
            message="API error",
            symbol="AAPL",
            instrument_type="equity_us",
            query="search query",
        )
        assert result == {
            "error": "API error",
            "source": "yahoo",
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "query": "search query",
        }

    def test_none_values_excluded(self):
        result = shared.error_payload(
            source="kis", message="error", symbol=None, instrument_type=None
        )
        assert "symbol" not in result
        assert "instrument_type" not in result


@pytest.mark.unit
class TestNormalizeRows:
    """Tests for _normalize_rows helper function."""

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        assert shared.normalize_rows(df) == []

    def test_single_row(self):
        df = pd.DataFrame([{"a": 1, "b": "text"}])
        result = shared.normalize_rows(df)
        assert result == [{"a": 1, "b": "text"}]

    def test_multiple_rows(self):
        df = pd.DataFrame([{"x": 1}, {"x": 2}, {"x": 3}])
        result = shared.normalize_rows(df)
        assert len(result) == 3
        assert result[0]["x"] == 1
        assert result[2]["x"] == 3

    def test_normalizes_values(self):
        import datetime

        df = pd.DataFrame(
            [
                {
                    "date": datetime.date(2024, 1, 15),
                    "value": float("nan"),
                    "count": 42,
                }
            ]
        )
        result = shared.normalize_rows(df)
        assert result[0]["date"] == "2024-01-15"
        assert result[0]["value"] is None
        assert result[0]["count"] == 42


@pytest.mark.unit
class TestSymbolNotFound:
    """Tests for symbol not found error handling."""

    @pytest.mark.asyncio
    async def test_get_quote_crypto_not_found(self, monkeypatch):
        tools = build_tools()
        # Return None for the symbol (not found)
        mock_fetch = AsyncMock(return_value={"KRW-INVALID": None})
        monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

        result = await tools["get_quote"]("KRW-INVALID")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "upbit"

    @pytest.mark.asyncio
    async def test_get_quote_korean_equity_not_found(self, monkeypatch):
        tools = build_tools()

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n):
                return pd.DataFrame()  # Empty DataFrame

        _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

        result = await tools["get_quote"]("999999")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "kis"

    @pytest.mark.asyncio
    async def test_get_quote_us_equity_not_found(self, monkeypatch):
        tools = build_tools()

        # Mock yfinance Ticker with None values (invalid symbol)
        class MockFastInfo:
            last_price = None
            regular_market_previous_close = None
            open = None
            day_high = None
            day_low = None
            last_volume = None

        class MockTicker:
            fast_info = MockFastInfo()

        monkeypatch.setattr("yfinance.Ticker", lambda symbol: MockTicker())

        result = await tools["get_quote"]("INVALID")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "yahoo"


# ---------------------------------------------------------------------------
# Technical Indicator Tests
# ---------------------------------------------------------------------------


def _sample_ohlcv_df(n: int = 250, include_date: bool = True) -> pd.DataFrame:
    """Create sample OHLCV DataFrame for indicator testing."""
    import datetime as dt

    import numpy as np

    np.random.seed(42)
    base_price = 100.0
    prices = base_price + np.cumsum(np.random.randn(n) * 2)

    df = pd.DataFrame(
        {
            "open": prices + np.random.randn(n) * 0.5,
            "high": prices + abs(np.random.randn(n) * 1.5),
            "low": prices - abs(np.random.randn(n) * 1.5),
            "close": prices,
            "volume": np.random.randint(1000, 10000, n),
        }
    )

    if include_date:
        # Generate dates going back from today
        end_date = dt.date.today()
        dates = [end_date - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
        df["date"] = dates

    return df


@pytest.mark.unit
class TestCalculateSMA:
    """Tests for _calculate_sma function."""

    def test_calculates_sma_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = market_data_indicators._calculate_sma(df["close"])

        assert "5" in result
        assert "20" in result
        assert "60" in result
        assert "120" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_sma(df["close"])

        assert result["5"] is not None
        assert result["20"] is None
        assert result["200"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_sma(df["close"], periods=[5, 10, 25])

        assert "5" in result
        assert "10" in result
        assert "25" in result
        assert len(result) == 3


@pytest.mark.unit
class TestCalculateEMA:
    """Tests for _calculate_ema function."""

    def test_calculates_ema_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = market_data_indicators._calculate_ema(df["close"])

        assert "5" in result
        assert "20" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_ema(df["close"])

        assert result["5"] is not None
        assert result["20"] is None

    def test_ema_differs_from_sma(self):
        df = _sample_ohlcv_df(50)
        sma = market_data_indicators._calculate_sma(df["close"], periods=[20])
        ema = market_data_indicators._calculate_ema(df["close"], periods=[20])

        # EMA gives more weight to recent prices, so values should differ
        assert sma["20"] != ema["20"]


@pytest.mark.unit
class TestCalculateRSI:
    """Tests for _calculate_rsi function."""

    def test_calculates_rsi(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_rsi(df["close"])

        assert "14" in result
        assert result["14"] is not None
        # RSI should be between 0 and 100
        assert 0 <= result["14"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_rsi(df["close"])

        assert result["14"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_rsi(df["close"], period=7)

        assert "7" in result
        assert result["7"] is not None


@pytest.mark.unit
class TestCalculateMACD:
    """Tests for _calculate_macd function."""

    def test_calculates_macd(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_macd(df["close"])

        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(20)
        result = market_data_indicators._calculate_macd(df["close"])

        assert result["macd"] is None
        assert result["signal"] is None
        assert result["histogram"] is None

    def test_histogram_equals_macd_minus_signal(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_macd(df["close"])

        assert result["macd"] is not None
        assert result["signal"] is not None
        assert result["histogram"] is not None
        expected_hist = result["macd"] - result["signal"]
        assert abs(result["histogram"] - expected_hist) < 0.01


@pytest.mark.unit
class TestCalculateBollinger:
    """Tests for _calculate_bollinger function."""

    def test_calculates_bollinger_bands(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_bollinger(df["close"])

        assert "upper" in result
        assert "middle" in result
        assert "lower" in result
        assert all(v is not None for v in result.values())
        # Upper > middle > lower
        assert result["upper"] is not None
        assert result["middle"] is not None
        assert result["lower"] is not None
        assert result["upper"] > result["middle"] > result["lower"]

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_bollinger(df["close"])

        assert result["upper"] is None
        assert result["middle"] is None
        assert result["lower"] is None

    def test_middle_equals_sma(self):
        df = _sample_ohlcv_df(50)
        bollinger = market_data_indicators._calculate_bollinger(df["close"], period=20)
        sma = market_data_indicators._calculate_sma(df["close"], periods=[20])

        assert bollinger["middle"] is not None
        assert sma["20"] is not None
        assert abs(bollinger["middle"] - sma["20"]) < 0.01


@pytest.mark.unit
class TestCalculateATR:
    """Tests for _calculate_atr function."""

    def test_calculates_atr(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_atr(
            df["high"], df["low"], df["close"]
        )

        assert "14" in result
        assert result["14"] is not None
        assert result["14"] > 0

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_atr(
            df["high"], df["low"], df["close"]
        )

        assert result["14"] is None


@pytest.mark.unit
class TestCalculatePivot:
    """Tests for _calculate_pivot function."""

    def test_calculates_pivot_points(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        assert "p" in result
        assert "r1" in result
        assert "r2" in result
        assert "r3" in result
        assert "s1" in result
        assert "s2" in result
        assert "s3" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(1)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        assert result["p"] is None
        assert result["r1"] is None
        assert result["s1"] is None

    def test_pivot_ordering(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_pivot(
            df["high"], df["low"], df["close"]
        )

        # R3 > R2 > R1 > P > S1 > S2 > S3
        assert result["r3"] is not None
        assert result["r2"] is not None
        assert result["r1"] is not None
        assert result["s1"] is not None
        assert result["s2"] is not None
        assert result["s3"] is not None
        assert result["r3"] > result["r2"] > result["r1"]
        assert result["s1"] > result["s2"] > result["s3"]


@pytest.mark.unit
class TestCalculateADX:
    """Tests for _calculate_adx function."""

    def test_calculates_adx(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"]
        )

        assert "adx" in result
        assert "plus_di" in result
        assert "minus_di" in result
        assert all(v is not None for v in result.values())
        assert result["adx"] is not None
        assert 0 <= result["adx"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"]
        )

        assert result["adx"] is None
        assert result["plus_di"] is None
        assert result["minus_di"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(60)
        result = market_data_indicators._calculate_adx(
            df["high"], df["low"], df["close"], period=10
        )

        assert result["adx"] is not None
        assert result["plus_di"] is not None
        assert result["minus_di"] is not None


@pytest.mark.unit
class TestCalculateStochRSI:
    """Tests for _calculate_stoch_rsi function."""

    def test_calculates_stoch_rsi(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(df["close"])

        assert "k" in result
        assert "d" in result
        assert result["k"] is not None
        assert result["d"] is not None
        assert 0 <= result["k"] <= 100
        assert 0 <= result["d"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_stoch_rsi(df["close"])

        assert result["k"] is None
        assert result["d"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=7, k_period=5, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None


@pytest.mark.unit
class TestCalculateOBV:
    """Tests for _calculate_obv function."""

    def test_calculates_obv(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._calculate_obv(df["close"], df["volume"])

        assert "obv" in result
        assert "signal" in result
        assert "divergence" in result
        assert result["obv"] is not None
        assert result["signal"] is not None
        assert result["divergence"] in ("bullish", "bearish", "none")

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = market_data_indicators._calculate_obv(df["close"], df["volume"])

        assert result["obv"] is None
        assert result["signal"] is None
        assert result["divergence"] is None

    def test_bullish_divergence_detected(self):
        n = 30
        close = pd.Series([100.0] * n)
        volume = pd.Series([1000.0] * n)
        close.iloc[-5:] = [100, 98, 96, 98, 95]
        volume.iloc[-5:] = [1000, 1000, 1000, 10000, 1000]

        result = market_data_indicators._calculate_obv(close, volume)

        assert result["divergence"] == "bullish"

    def test_bearish_divergence_detected(self):
        n = 30
        close = pd.Series([95.0] * n)
        volume = pd.Series([1000.0] * n)
        close.iloc[-5:] = [95, 97, 99, 97, 100]
        volume.iloc[-5:] = [1000, 1000, 1000, 10000, 1000]

        result = market_data_indicators._calculate_obv(close, volume)

        assert result["divergence"] == "bearish"

    def test_signal_is_ema_not_sma(self):
        close = pd.Series([100.0 + i * 0.5 for i in range(30)])
        volume = pd.Series([1000.0] * 30)

        result = market_data_indicators._calculate_obv(close, volume, signal_period=10)

        assert result["signal"] is not None
        direction = np.where(
            close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0)
        )
        obv = (volume * direction).cumsum()
        expected_signal = obv.ewm(span=10, adjust=False).mean().iloc[-1]
        assert result["signal"] == pytest.approx(round(float(expected_signal), 2), abs=0.01)


@pytest.mark.unit
class TestADXRegression:
    """Regression tests for ADX DM calculation fix."""

    def test_dm_independent_filtering(self):
        high = pd.Series([100, 105, 100, 100, 102, 101])
        low = pd.Series([95, 94, 95, 95, 94, 94])
        close = pd.Series([98, 103, 98, 98, 100, 99])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["adx"] is not None
        assert result["plus_di"] is not None
        assert result["minus_di"] is not None

    def test_up_move_greater_than_down_move(self):
        high = pd.Series([100, 105, 100, 100, 102, 101])
        low = pd.Series([95, 94, 95, 95, 94, 94])
        close = pd.Series([98, 103, 98, 98, 100, 99])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["plus_di"] is not None
        assert result["minus_di"] is not None

    def test_down_move_greater_than_up_move(self):
        high = pd.Series([100, 100, 100, 100, 100, 100])
        low = pd.Series([95, 90, 95, 95, 92, 92])
        close = pd.Series([98, 93, 98, 98, 96, 96])

        result = market_data_indicators._calculate_adx(high, low, close, period=2)

        assert result["plus_di"] is not None
        assert result["minus_di"] is not None


@pytest.mark.unit
class TestStochRSIRegression:
    """Regression tests for Stoch RSI calculation fix."""

    def test_returns_values_at_minimum_length_boundary(self):
        boundary_len = 14 + 3 + 3
        df = _sample_ohlcv_df(boundary_len)

        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None

    def test_uses_rsi_period_for_rolling_min_max(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None
        assert 0 <= result["k"] <= 100
        assert 0 <= result["d"] <= 100

    def test_k_is_smoothed_stoch_rsi(self):
        df = _sample_ohlcv_df(100)

        result = market_data_indicators._calculate_stoch_rsi(
            df["close"], rsi_period=14, k_period=3, d_period=3
        )

        assert result["k"] is not None
        assert result["d"] is not None


@pytest.mark.unit
class TestOBVRegression:
    """Regression tests for OBV calculation fix."""

    def test_signal_uses_ema(self):
        close = pd.Series([100.0 + i for i in range(30)])
        volume = pd.Series([1000.0] * 30)

        result = market_data_indicators._calculate_obv(close, volume, signal_period=10)

        direction = np.where(
            close > close.shift(1), 1, np.where(close < close.shift(1), -1, 0)
        )
        obv = (volume * direction).cumsum()
        expected_signal = obv.ewm(span=10, adjust=False).mean().iloc[-1]

        assert result["signal"] is not None
        assert abs(result["signal"] - expected_signal) < 0.01

    def test_divergence_uses_lookback_plus_one_index(self):
        close = pd.Series([100.0, 110.0, 90.0, 91.0, 92.0, 93.0, 94.0, 95.0, 96.0, 97.0, 98.0, 95.0])
        volume = pd.Series([1000.0] * len(close))

        result = market_data_indicators._calculate_obv(close, volume, signal_period=5)

        # With lookback=10 and index -lookback-1, price_change<0 while obv_change>0 => bullish
        assert result["divergence"] == "bullish"


@pytest.mark.unit
class TestComputeIndicators:
    """Tests for _compute_indicators function."""

    def test_computes_single_indicator(self):
        df = _sample_ohlcv_df(50)
        result = market_data_indicators._compute_indicators(df, ["rsi"])

        assert "rsi" in result
        assert len(result) == 1

    def test_computes_multiple_indicators(self):
        df = _sample_ohlcv_df(100)
        result = market_data_indicators._compute_indicators(
            df, ["sma", "ema", "rsi", "macd"]
        )

        assert "sma" in result
        assert "ema" in result
        assert "rsi" in result
        assert "macd" in result

    def test_computes_all_indicators(self):
        df = _sample_ohlcv_df(250)
        all_indicators = [
            "sma",
            "ema",
            "rsi",
            "macd",
            "bollinger",
            "atr",
            "pivot",
            "adx",
            "stoch_rsi",
            "obv",
        ]
        result = market_data_indicators._compute_indicators(df, all_indicators)

        for indicator in all_indicators:
            assert indicator in result

    def test_raises_on_missing_columns(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["atr"])

    def test_raises_on_missing_columns_for_adx(self):
        df = pd.DataFrame({"close": [1, 2, 3], "high": [4, 5, 6]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["adx"])

    def test_raises_on_missing_columns_for_obv(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            market_data_indicators._compute_indicators(df, ["obv"])


@pytest.mark.asyncio
class TestAnalyzeStock:
    """Test analyze_stock tool."""

    async def test_recommendation_generation_kr(self, monkeypatch):
        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "indicators": {
                "indicators": {
                    "rsi": {"14": 45.0},
                    "bollinger": {"lower": 74000},
                }
            },
            "support_resistance": {
                "supports": [{"price": 73000}],
                "resistances": [{"price": 77000, "strength": "medium"}],
            },
            "opinions": {
                "consensus": {
                    "buy_count": 2,
                    "avg_target_price": 85000,
                    "current_price": 75000,
                },
            },
        }

        # Test _build_recommendation_for_equity directly
        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_kr"
        )

        assert recommendation is not None
        rec = recommendation
        assert "action" in rec
        assert "confidence" in rec
        assert "buy_zones" in rec
        assert "sell_targets" in rec
        assert "stop_loss" in rec
        assert "reasoning" in rec

    async def test_recommendation_not_included_crypto(self, monkeypatch):
        tools = build_tools()

        mock_analysis = {
            "symbol": "KRW-BTC",
            "market_type": "crypto",
            "source": "upbit",
            "quote": {"current_price": 80000000},
            "indicators": {
                "rsi": 50.0,
                "bollinger_bands": {
                    "lower": 78000000,
                    "middle": 80000000,
                    "upper": 82000000,
                },
            },
            "support_resistance": {
                "supports": [{"price": 75000000}],
                "resistances": [{"price": 85000000}],
            },
        }

        _patch_runtime_attr(
            monkeypatch, "_analyze_stock_impl", lambda s, m, i: mock_analysis
        )

        result = await tools["analyze_stock"]("KRW-BTC", market="crypto")

        assert "recommendation" not in result

    async def test_us_opinions_schema_consistency(self, monkeypatch):
        """Test that US opinions have the 'opinions' key."""
        # Mock yfinance data
        mock_opinions = {
            "instrument_type": "equity_us",
            "source": "yfinance",
            "symbol": "AAPL",
            "count": 2,
            "opinions": [
                {
                    "firm": "Firm A",
                    "rating": "buy",
                    "date": "2024-01-01",
                    "target_price": 200,
                },
                {"firm": "Firm B", "rating": "hold", "date": "2024-01-02"},
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 1,
                "sell_count": 0,
                "total_count": 2,
                "avg_target_price": 200,
                "current_price": 150,
            },
        }

        async def mock_fetch(symbol, limit):
            return mock_opinions

        _patch_runtime_attr(
            monkeypatch,
            "_fetch_investment_opinions_yfinance",
            mock_fetch,
        )

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        # Only opinions key should exist
        assert "opinions" in result
        assert len(result["opinions"]) == 2

    async def test_numeric_symbol_normalization_analyze_stock(self, monkeypatch):
        """Test that analyze_stock accepts numeric symbols and normalizes them."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
        }

        _patch_runtime_attr(
            monkeypatch, "_analyze_stock_impl", lambda s, m, i: mock_analysis
        )

        # Test with integer input
        result = await tools["analyze_stock"](5930, market="kr")
        assert result["symbol"] == "005930"

        # Test with string input (should also work)
        result = await tools["analyze_stock"]("5930", market="kr")
        assert result["symbol"] == "005930"

    async def test_numeric_symbol_normalization_analyze_portfolio(self, monkeypatch):
        """Test that analyze_portfolio accepts numeric symbols and normalizes them."""
        tools = build_tools()

        def mock_impl(symbol, market, include_peers):
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "kis",
                "quote": {"price": 75000},
            }

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", mock_impl)

        # Test with mixed numeric and string symbols
        result = await tools["analyze_portfolio"](
            symbols=[12450, "005930"], market="kr"
        )

        assert "results" in result
        # Both symbols should be normalized to 6-digit strings
        assert "012450" in result["results"]
        assert "005930" in result["results"]


@pytest.mark.asyncio
class TestGetValuation:
    """Test get_valuation tool."""

    async def test_successful_valuation_fetch(self, monkeypatch):
        """Test successful valuation fetch for Korean stock."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "005930",
            "name": "삼성전자",
            "current_price": 75000,
            "per": 12.5,
            "pbr": 1.2,
            "roe": 18.5,
            "roe_controlling": 17.2,
            "dividend_yield": 0.02,
            "high_52w": 90000,
            "low_52w": 60000,
            "current_position_52w": 0.5,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("005930")

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 75000
        assert result["per"] == 12.5
        assert result["pbr"] == 1.2
        assert result["roe"] == 18.5
        assert result["roe_controlling"] == 17.2
        assert result["dividend_yield"] == 0.02
        assert result["high_52w"] == 90000
        assert result["low_52w"] == 60000
        assert result["current_position_52w"] == 0.5
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"

    async def test_successful_us_valuation_fetch(self, monkeypatch):
        """Test successful valuation fetch for US stock via yfinance."""
        tools = build_tools()

        mock_info = {
            "shortName": "Apple Inc.",
            "currentPrice": 185.5,
            "trailingPE": 28.5,
            "priceToBook": 45.2,
            "returnOnEquity": 1.473,
            "dividendYield": 0.005,
            "fiftyTwoWeekHigh": 199.62,
            "fiftyTwoWeekLow": 164.08,
        }

        class MockTicker:
            @property
            def info(self):
                return mock_info

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["current_price"] == 185.5
        assert result["per"] == 28.5
        assert result["pbr"] == 45.2
        assert result["roe"] == 147.3
        assert result["dividend_yield"] == 0.005
        assert result["high_52w"] == 199.62
        assert result["low_52w"] == 164.08
        assert result["current_position_52w"] == 0.6
        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "yfinance"

    async def test_us_valuation_with_explicit_market(self, monkeypatch):
        """Test US valuation with explicit market parameter."""
        tools = build_tools()

        mock_info = {
            "shortName": "NVIDIA Corp",
            "currentPrice": 500.0,
            "trailingPE": 60.0,
            "priceToBook": 30.0,
            "returnOnEquity": 0.85,
            "dividendYield": 0.001,
            "fiftyTwoWeekHigh": 550.0,
            "fiftyTwoWeekLow": 300.0,
        }

        class MockTicker:
            @property
            def info(self):
                return mock_info

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("NVDA", market="us")

        assert result["symbol"] == "NVDA"
        assert result["instrument_type"] == "equity_us"
        assert result["roe"] == 85.0

    async def test_rejects_crypto(self):
        """Test that crypto symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="cryptocurrencies"):
            await tools["get_valuation"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_valuation"]("")

    async def test_valuation_with_null_values(self, monkeypatch):
        """Test valuation response with some null values."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "298040",
            "name": "효성중공업",
            "current_price": 450000,
            "per": None,
            "pbr": 2.1,
            "roe": None,
            "roe_controlling": None,
            "dividend_yield": 0.005,
            "high_52w": 500000,
            "low_52w": 200000,
            "current_position_52w": 0.83,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("298040")

        assert result["symbol"] == "298040"
        assert result["per"] is None
        assert result["roe"] is None
        assert result["current_position_52w"] == 0.83

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_valuation(code):
            raise Exception("Network error")

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("005930")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"

    async def test_us_error_handling(self, monkeypatch):
        """Test error handling when yfinance fetch fails."""
        tools = build_tools()

        class MockTicker:
            @property
            def info(self):
                raise Exception("API error")

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("AAPL")

        assert "error" in result
        assert result["source"] == "yfinance"
        assert result["symbol"] == "AAPL"
        assert result["instrument_type"] == "equity_us"

    async def test_invalid_market_raises_error(self):
        """Test that invalid market raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="must be 'us' or 'kr'"):
            await tools["get_valuation"]("AAPL", market="invalid")


@pytest.mark.asyncio
class TestGetShortInterest:
    """Test get_short_interest tool."""

    async def test_successful_short_interest_fetch(self, monkeypatch):
        """Test successful short interest fetch for Korean stock."""
        tools = build_tools()

        mock_short_interest = {
            "symbol": "005930",
            "name": "삼성전자",
            "short_data": [
                {
                    "date": "2024-01-15",
                    "short_amount": 1_000_000_000,
                    "total_amount": 20_000_000_000,
                    "short_ratio": 5.0,
                    "short_volume": None,
                    "total_volume": None,
                },
                {
                    "date": "2024-01-14",
                    "short_amount": 800_000_000,
                    "total_amount": 15_000_000_000,
                    "short_ratio": 5.33,
                    "short_volume": None,
                    "total_volume": None,
                },
            ],
            "avg_short_ratio": 5.17,
            "short_balance": {
                "balance_shares": 1_234_567,
                "balance_amount": 98_765_432_100,
                "balance_ratio": 0.5,
            },
        }

        async def mock_fetch_short_interest(code, days):
            return mock_short_interest

        monkeypatch.setattr(
            naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert len(result["short_data"]) == 2
        assert result["short_data"][0]["date"] == "2024-01-15"
        assert result["short_data"][0]["short_amount"] == 1_000_000_000
        assert result["short_data"][0]["short_ratio"] == 5.0
        assert result["avg_short_ratio"] == 5.17
        assert result["short_balance"]["balance_shares"] == 1_234_567

    async def test_rejects_us_equity(self):
        """Test that US equity symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_short_interest"]("AAPL")

    async def test_rejects_crypto(self):
        """Test that crypto symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_short_interest"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_short_interest"]("")

    async def test_days_limit_capped(self, monkeypatch):
        """Test that days parameter is capped at 60."""
        tools = build_tools()

        captured_days = None

        async def mock_fetch_short_interest(code, days):
            nonlocal captured_days
            captured_days = days
            return {
                "symbol": code,
                "name": "테스트",
                "short_data": [],
                "avg_short_ratio": None,
            }

        monkeypatch.setattr(
            naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        await tools["get_short_interest"]("005930", days=100)

        assert captured_days == 60

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_short_interest(code, days):
            raise Exception("KRX API error")

        monkeypatch.setattr(
            naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930")

        assert "error" in result
        assert result["source"] == "krx"
        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"

    async def test_empty_short_data(self, monkeypatch):
        """Test response with no short data."""
        tools = build_tools()

        mock_short_interest = {
            "symbol": "000000",
            "name": "테스트종목",
            "short_data": [],
            "avg_short_ratio": None,
        }

        async def mock_fetch_short_interest(code, days):
            return mock_short_interest

        monkeypatch.setattr(
            naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("000000")

        assert result["symbol"] == "000000"
        assert result["short_data"] == []
        assert result["avg_short_ratio"] is None
        assert "short_balance" not in result


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetKimchiPremium:
    """Test get_kimchi_premium tool."""

    def _patch_all(self, monkeypatch, upbit_prices, binance_resp, exchange_rate):
        """Helper to monkeypatch Upbit, Binance, and exchange rate."""

        async def mock_upbit(markets):
            return upbit_prices

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            mock_upbit,
        )

        class MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

            def __init__(self, data):
                self._data = data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "binance" in url:
                    return MockResponse(binance_resp)
                # exchange rate
                return MockResponse({"rates": {"KRW": exchange_rate}})

        _patch_httpx_async_client(monkeypatch, MockClient)

    async def test_single_symbol(self, monkeypatch):
        """Test kimchi premium for a single coin."""
        tools = build_tools()

        self._patch_all(
            monkeypatch,
            upbit_prices={"KRW-BTC": 150_000_000},
            binance_resp=[{"symbol": "BTCUSDT", "price": "102000.50"}],
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]("BTC")

        assert result["source"] == "upbit+binance"
        assert result["exchange_rate"] == 1450.0
        assert result["count"] == 1
        item = result["data"][0]
        assert item["symbol"] == "BTC"
        assert item["upbit_krw"] == 150_000_000
        assert item["binance_usdt"] == 102000.50
        # (150_000_000 - 102000.50*1450) / (102000.50*1450) * 100
        expected_premium = round(
            (150_000_000 - 102000.50 * 1450) / (102000.50 * 1450) * 100, 2
        )
        assert item["premium_pct"] == expected_premium

    async def test_default_symbols(self, monkeypatch):
        """Test batch fetch when symbol is omitted."""
        tools = build_tools()

        _patch_runtime_attr(
            monkeypatch,
            "_resolve_batch_crypto_symbols",
            AsyncMock(return_value=["BTC", "ETH"]),
        )

        upbit = {"KRW-BTC": 150_000_000, "KRW-ETH": 4_500_000}
        binance = [
            {"symbol": "BTCUSDT", "price": "102000"},
            {"symbol": "ETHUSDT", "price": "3050"},
        ]

        self._patch_all(
            monkeypatch,
            upbit_prices=upbit,
            binance_resp=binance,
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]()

        assert isinstance(result, list)
        assert len(result) == 2
        symbols = [d["symbol"] for d in result]
        assert symbols == ["BTC", "ETH"]
        assert result[0]["upbit_price"] == 150_000_000
        assert result[0]["binance_price"] == 102000.0
        assert "premium_pct" in result[0]

    async def test_strips_krw_prefix(self, monkeypatch):
        """Test that KRW- prefix is stripped from symbol."""
        tools = build_tools()

        self._patch_all(
            monkeypatch,
            upbit_prices={"KRW-ETH": 4_500_000},
            binance_resp=[{"symbol": "ETHUSDT", "price": "3050"}],
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]("KRW-ETH")

        assert result["count"] == 1
        assert result["data"][0]["symbol"] == "ETH"

    async def test_error_handling(self, monkeypatch):
        """Test error handling when external API fails."""
        tools = build_tools()

        async def mock_upbit(markets):
            raise Exception("Upbit API down")

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            mock_upbit,
        )

        result = await tools["get_kimchi_premium"]("BTC")

        assert "error" in result
        assert result["source"] == "upbit+binance"
        assert result["instrument_type"] == "crypto"


# ---------------------------------------------------------------------------
# Funding Rate Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFundingRate:
    """Test get_funding_rate tool."""

    def _patch_binance(self, monkeypatch, premium_resp, history_resp):
        """Helper to monkeypatch Binance futures API responses."""

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "premiumIndex" in url:
                    return MockResponse(premium_resp)
                return MockResponse(history_resp)

        _patch_httpx_async_client(monkeypatch, MockClient)

    async def test_successful_fetch(self, monkeypatch):
        """Test successful funding rate fetch for BTC."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1707235200000,  # 2024-02-06T16:00:00Z
        }
        history = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "fundingTime": 1707206400000,  # 2024-02-06T08:00:00Z
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015",
                "fundingTime": 1707177600000,  # 2024-02-06T00:00:00Z
            },
        ]

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTC")

        assert result["symbol"] == "BTCUSDT"
        assert result["current_funding_rate"] == 0.0001
        assert result["current_funding_rate_pct"] == 0.01
        assert result["next_funding_time"] is not None
        assert len(result["funding_history"]) == 2
        assert result["funding_history"][0]["rate"] == 0.0001
        assert result["funding_history"][0]["rate_pct"] == 0.01
        assert result["avg_funding_rate_pct"] is not None
        assert "interpretation" in result

    async def test_strips_krw_prefix(self, monkeypatch):
        """Test that KRW- prefix is stripped from symbol."""
        tools = build_tools()

        premium = {
            "symbol": "ETHUSDT",
            "lastFundingRate": "0.0002",
            "nextFundingTime": 0,
        }
        history = []

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("KRW-ETH")

        assert result["symbol"] == "ETHUSDT"

    async def test_strips_usdt_suffix(self, monkeypatch):
        """Test that USDT suffix is stripped from symbol."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }
        history = []

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTCUSDT")

        assert result["symbol"] == "BTCUSDT"

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_funding_rate"]("")

    async def test_batch_fetch_when_symbol_is_none(self, monkeypatch):
        """Test funding-rate batch response when symbol is omitted."""
        tools = build_tools()

        _patch_runtime_attr(
            monkeypatch,
            "_resolve_batch_crypto_symbols",
            AsyncMock(return_value=["BTC", "ETH"]),
        )

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                assert "premiumIndex" in url
                return MockResponse(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "lastFundingRate": "0.0001",
                            "nextFundingTime": 1707235200000,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "lastFundingRate": "-0.0002",
                            "nextFundingTime": 1707235200000,
                        },
                        {
                            "symbol": "SOLUSDT",
                            "lastFundingRate": "0.0003",
                            "nextFundingTime": 1707235200000,
                        },
                    ]
                )

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_funding_rate"]()

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["symbol"] == "BTC"
        assert result[0]["funding_rate"] == 0.0001
        assert result[0]["next_funding_time"] is not None
        assert "interpretation" in result[0]

    async def test_limit_capped_at_100(self, monkeypatch):
        """Test that limit is capped at 100."""
        tools = build_tools()

        captured_params = {}

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "fundingRate" in url and "premiumIndex" not in url:
                    captured_params.update(params or {})
                    return MockResponse([])
                return MockResponse(
                    {
                        "symbol": "BTCUSDT",
                        "lastFundingRate": "0.0001",
                        "nextFundingTime": 0,
                    }
                )

        _patch_httpx_async_client(monkeypatch, MockClient)

        await tools["get_funding_rate"]("BTC", limit=200)

        assert captured_params["limit"] == 100

    async def test_error_handling(self, monkeypatch):
        """Test error handling when Binance API fails."""
        tools = build_tools()

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                raise Exception("Binance API down")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_funding_rate"]("BTC")

        assert "error" in result
        assert result["source"] == "binance"
        assert result["symbol"] == "BTCUSDT"
        assert result["instrument_type"] == "crypto"

    async def test_avg_funding_rate_calculation(self, monkeypatch):
        """Test average funding rate calculation."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }
        history = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0002",
                "fundingTime": 1707206400000,
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0004",
                "fundingTime": 1707177600000,
            },
        ]

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTC", limit=2)

        # avg = (0.0002 + 0.0004) / 2 * 100 = 0.03
        assert result["avg_funding_rate_pct"] == 0.03

    async def test_empty_history(self, monkeypatch):
        """Test response with empty history."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }

        self._patch_binance(monkeypatch, premium, [])

        result = await tools["get_funding_rate"]("BTC")

        assert result["funding_history"] == []
        assert result["avg_funding_rate_pct"] is None

    async def test_interpretation_present(self, monkeypatch):
        """Test that interpretation is included in response."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }

        self._patch_binance(monkeypatch, premium, [])

        result = await tools["get_funding_rate"]("BTC")

        assert "interpretation" in result
        assert "positive" in result["interpretation"]
        assert "negative" in result["interpretation"]


# ---------------------------------------------------------------------------
# Market Index Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseNaverNum:
    """Tests for _parse_naver_num and _parse_naver_int."""

    def test_none(self):
        assert fundamentals_sources_naver._parse_naver_num(None) is None
        assert fundamentals_sources_naver._parse_naver_int(None) is None

    def test_numeric(self):
        assert fundamentals_sources_naver._parse_naver_num(1234.5) == 1234.5
        assert fundamentals_sources_naver._parse_naver_num(100) == 100.0
        assert fundamentals_sources_naver._parse_naver_int(42) == 42

    def test_string_with_commas(self):
        assert fundamentals_sources_naver._parse_naver_num("2,450.50") == 2450.50
        assert fundamentals_sources_naver._parse_naver_num("-45.30") == -45.30
        assert fundamentals_sources_naver._parse_naver_int("450,000,000") == 450000000

    def test_invalid_string(self):
        assert fundamentals_sources_naver._parse_naver_num("abc") is None
        assert fundamentals_sources_naver._parse_naver_int("abc") is None


@pytest.mark.unit
class TestIndexMeta:
    """Tests for _INDEX_META and _DEFAULT_INDICES."""

    def test_all_default_indices_have_meta(self):
        for sym in fundamentals_sources_indices._DEFAULT_INDICES:
            assert sym in fundamentals_sources_indices._INDEX_META

    def test_korean_indices_have_naver_code(self):
        for sym in ("KOSPI", "KOSDAQ"):
            meta = fundamentals_sources_indices._INDEX_META[sym]
            assert meta["source"] == "naver"
            assert "naver_code" in meta

    def test_us_indices_have_yf_ticker(self):
        for sym in ("SPX", "NASDAQ", "DJI"):
            meta = fundamentals_sources_indices._INDEX_META[sym]
            assert meta["source"] == "yfinance"
            assert "yf_ticker" in meta

    def test_aliases(self):
        assert (
            fundamentals_sources_indices._INDEX_META["SPX"]["yf_ticker"]
            == fundamentals_sources_indices._INDEX_META["SP500"]["yf_ticker"]
        )
        assert (
            fundamentals_sources_indices._INDEX_META["DJI"]["yf_ticker"]
            == fundamentals_sources_indices._INDEX_META["DOW"]["yf_ticker"]
        )


# ---------------------------------------------------------------------------
# get_market_index Tool
# ---------------------------------------------------------------------------


def _naver_basic_json(
    close="2,450.50",
    change="-45.30",
    change_pct="-1.82",
    open_price="2,495.00",
    high="2,498.00",
    low="2,440.00",
    volume="450,000,000",
):
    return {
        "closePrice": close,
        "compareToPreviousClosePrice": change,
        "fluctuationsRatio": change_pct,
        "openPrice": open_price,
        "highPrice": high,
        "lowPrice": low,
        "accumulatedTradingVolume": volume,
    }


def _naver_price_history(n=3):
    items = []
    for i in range(n):
        items.append(
            {
                "localTradedAt": f"2026-02-0{i + 1}",
                "closePrice": f"{2400 + i * 10}",
                "openPrice": f"{2390 + i * 10}",
                "highPrice": f"{2420 + i * 10}",
                "lowPrice": f"{2380 + i * 10}",
                "accumulatedTradingVolume": f"{400_000_000 + i * 10_000_000}",
            }
        )
    return items


class _FakeResponse:
    """Fake httpx.Response for mocking."""

    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=None,
                response=self,  # type: ignore[arg-type]
            )

    def json(self):
        return self._json_data


@pytest.mark.asyncio
class TestGetMarketIndex:
    """Tests for get_market_index tool."""

    def _patch_naver(self, monkeypatch, basic_json, price_json):
        """Patch httpx.AsyncClient.get for naver API calls.

        Note: _fetch_index_kr_current calls both /basic and /price (pageSize=1),
        while _fetch_index_kr_history calls /price with a larger pageSize.
        """
        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            if "/basic" in url:
                return _FakeResponse(basic_json)
            elif "/price" in url:
                return _FakeResponse(price_json)
            raise ValueError(f"Unexpected URL: {url}")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    def _patch_yfinance(self, monkeypatch, last_price=5500.0, prev_close=5450.0):
        """Patch yfinance for US index."""

        class MockFastInfo:
            pass

        info = MockFastInfo()
        info.last_price = last_price
        info.regular_market_previous_close = prev_close
        info.open = 5460.0
        info.day_high = 5510.0
        info.day_low = 5430.0
        info.last_volume = 3_500_000_000

        class MockTicker:
            fast_info = info

        monkeypatch.setattr("yfinance.Ticker", lambda symbol: MockTicker())

    def _patch_yf_download(self, monkeypatch, rows=3):
        """Patch yf.download for US index history."""
        dates = pd.date_range("2026-02-01", periods=rows, freq="D")
        df = pd.DataFrame(
            {
                "Date": dates,
                "Open": [5460 + i * 10 for i in range(rows)],
                "High": [5510 + i * 10 for i in range(rows)],
                "Low": [5430 + i * 10 for i in range(rows)],
                "Close": [5500 + i * 10 for i in range(rows)],
                "Volume": [3_500_000_000 + i * 100_000 for i in range(rows)],
            }
        ).set_index("Date")

        monkeypatch.setattr("yfinance.download", lambda *a, **kw: df)

    async def test_single_kr_index(self, monkeypatch):
        """Test fetching a single Korean index (KOSPI)."""
        tools = build_tools()
        basic = _naver_basic_json()
        history = _naver_price_history(3)
        # _fetch_index_kr_current calls /price?pageSize=1 and /basic
        # _fetch_index_kr_history calls /price with the full count
        # Both share the same mock that returns `history` for any /price call
        self._patch_naver(monkeypatch, basic, history)

        result = await tools["get_market_index"](symbol="KOSPI")

        assert "indices" in result
        assert len(result["indices"]) == 1
        idx = result["indices"][0]
        assert idx["symbol"] == "KOSPI"
        assert idx["name"] == "코스피"
        assert idx["current"] == 2450.50
        assert idx["change"] == -45.30
        assert idx["change_pct"] == -1.82
        assert idx["source"] == "naver"
        # open/high/low come from the first price record
        assert idx["open"] == 2390.0
        assert idx["high"] == 2420.0
        assert idx["low"] == 2380.0

        assert "history" in result
        assert len(result["history"]) == 3
        assert result["history"][0]["date"] == "2026-02-01"

    async def test_single_us_index(self, monkeypatch):
        """Test fetching a single US index (NASDAQ)."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch, last_price=17500.0, prev_close=17400.0)
        self._patch_yf_download(monkeypatch, rows=5)

        result = await tools["get_market_index"](symbol="NASDAQ")

        assert "indices" in result
        assert len(result["indices"]) == 1
        idx = result["indices"][0]
        assert idx["symbol"] == "NASDAQ"
        assert idx["name"] == "NASDAQ Composite"
        assert idx["current"] == 17500.0
        assert idx["change"] == 100.0
        assert idx["change_pct"] == pytest.approx(0.57, abs=0.01)
        assert idx["source"] == "yfinance"

        assert "history" in result
        assert len(result["history"]) == 5

    async def test_all_indices_no_symbol(self, monkeypatch):
        """Test fetching all major indices when no symbol specified."""
        tools = build_tools()

        # Patch both naver (for KOSPI, KOSDAQ) and yfinance (for SPX, NASDAQ)
        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            if "/basic" in url:
                return _FakeResponse(_naver_basic_json())
            elif "/price" in url:
                return _FakeResponse(_naver_price_history(1))
            raise ValueError(f"Unexpected URL: {url}")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
        self._patch_yfinance(monkeypatch)

        result = await tools["get_market_index"]()

        assert "indices" in result
        assert len(result["indices"]) == 4
        assert "history" not in result

        # Verify we got both Korean and US indices
        symbols = [idx.get("symbol") for idx in result["indices"]]
        assert "KOSPI" in symbols
        assert "KOSDAQ" in symbols

    async def test_alias_sp500(self, monkeypatch):
        """Test SP500 alias resolves to same as SPX."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch)

        result = await tools["get_market_index"](symbol="SP500")

        assert result["indices"][0]["symbol"] == "SP500"
        assert result["indices"][0]["name"] == "S&P 500"

    async def test_alias_dow(self, monkeypatch):
        """Test DOW alias resolves to same as DJI."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch)

        result = await tools["get_market_index"](symbol="DOW")

        assert result["indices"][0]["symbol"] == "DOW"
        assert result["indices"][0]["name"] == "다우존스"

    async def test_unknown_symbol_raises_error(self):
        """Test that unknown index symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Unknown index symbol"):
            await tools["get_market_index"](symbol="UNKNOWN")

    async def test_invalid_period_raises_error(self):
        """Test that invalid period raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="period must be"):
            await tools["get_market_index"](symbol="KOSPI", period="hour")

    async def test_case_insensitive_symbol(self, monkeypatch):
        """Test that symbol is case-insensitive."""
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="kospi")

        assert result["indices"][0]["symbol"] == "KOSPI"

    async def test_count_capped_at_100(self, monkeypatch):
        """Test that count is capped at 100."""
        tools = build_tools()
        history_items = _naver_price_history(3)
        self._patch_naver(monkeypatch, _naver_basic_json(), history_items)

        result = await tools["get_market_index"](symbol="KOSPI", count=500)

        # Should not raise, count is internally capped
        assert "indices" in result

    async def test_count_minimum_1(self, monkeypatch):
        """Test that count minimum is 1."""
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(1))

        result = await tools["get_market_index"](symbol="KOSPI", count=-5)

        assert "indices" in result

    async def test_period_week(self, monkeypatch):
        """Test weekly period."""
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="KOSDAQ", period="week")

        assert "history" in result

    async def test_period_month(self, monkeypatch):
        """Test monthly period."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch, rows=3)

        result = await tools["get_market_index"](symbol="SPX", period="month")

        assert "history" in result

    async def test_error_returns_error_payload(self, monkeypatch):
        """Test that API errors return error payload."""
        tools = build_tools()

        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            raise RuntimeError("naver API down")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

        result = await tools["get_market_index"](symbol="KOSPI")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "KOSPI"

    async def test_all_indices_partial_failure(self, monkeypatch):
        """Test that partial failures in bulk query still return data."""
        tools = build_tools()

        import httpx as _httpx

        # Naver fails, yfinance succeeds
        async def fake_get(self_cli, url, **kwargs):
            raise RuntimeError("naver down")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
        self._patch_yfinance(monkeypatch)

        result = await tools["get_market_index"]()

        assert len(result["indices"]) == 4
        # Korean indices should have errors
        kr_results = [
            idx for idx in result["indices"] if idx.get("symbol") in ("KOSPI", "KOSDAQ")
        ]
        for kr in kr_results:
            assert "error" in kr

    async def test_us_history_empty_df(self, monkeypatch):
        """Test US index with empty download result."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        monkeypatch.setattr("yfinance.download", lambda *a, **kw: pd.DataFrame())

        result = await tools["get_market_index"](symbol="DJI")

        assert result["history"] == []

    async def test_strip_whitespace_symbol(self, monkeypatch):
        """Test that whitespace around symbol is stripped."""
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="  KOSPI  ")

        assert result["indices"][0]["symbol"] == "KOSPI"


# ---------------------------------------------------------------------------
# _calculate_fibonacci unit tests
# ---------------------------------------------------------------------------


def _fib_df_uptrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where low comes first, then high (uptrend)."""
    import datetime as dt

    import numpy as np

    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 100 up to ~200
    close = np.linspace(100, 200, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 1,
            "high": close + 2,
            "low": close - 3,
            "close": close,
            "volume": [1000] * n,
        }
    )


def _fib_df_downtrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where high comes first, then low (downtrend)."""
    import datetime as dt

    import numpy as np

    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 200 down to ~100
    close = np.linspace(200, 100, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close + 1,
            "high": close + 3,
            "low": close - 2,
            "close": close,
            "volume": [1000] * n,
        }
    )


@pytest.mark.unit
class TestCalculateFibonacci:
    """Tests for _calculate_fibonacci helper."""

    def test_uptrend_retracement_from_high(self):
        df = _fib_df_uptrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "retracement_from_high"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing high, 100% level = swing low
        assert result["levels"]["0.0"] > result["levels"]["1.0"]

    def test_downtrend_bounce_from_low(self):
        df = _fib_df_downtrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "bounce_from_low"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing low, 100% level = swing high
        assert result["levels"]["0.0"] < result["levels"]["1.0"]

    def test_all_seven_levels_present(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        expected_keys = {"0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0"}
        assert set(result["levels"].keys()) == expected_keys

    def test_nearest_support_and_resistance(self):
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        swing_low = float(df["low"].min())
        mid = (swing_high + swing_low) / 2
        result = market_data_indicators._calculate_fibonacci(df, mid)

        if result["nearest_support"] is not None:
            assert result["nearest_support"]["price"] < mid
        if result["nearest_resistance"] is not None:
            assert result["nearest_resistance"]["price"] > mid

    def test_dates_are_strings(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        assert isinstance(result["swing_high"]["date"], str)
        assert isinstance(result["swing_low"]["date"], str)
        # ISO date format check
        assert len(result["swing_high"]["date"]) == 10
        assert len(result["swing_low"]["date"]) == 10

    def test_price_at_exact_level_no_crash(self):
        """If current price matches a level exactly, no crash."""
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        result = market_data_indicators._calculate_fibonacci(df, swing_high)

        assert result["current_price"] == swing_high


# ---------------------------------------------------------------------------
# get_sector_peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetSectorPeers:
    async def test_raises_on_empty_symbol(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_sector_peers"]("")

    async def test_raises_on_crypto_symbol(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_sector_peers"]("KRW-BTC")

    async def test_raises_on_invalid_market(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="market must be"):
            await tools["get_sector_peers"]("005930", market="invalid")

    async def test_korean_equity_success(self, monkeypatch):
        tools = build_tools()

        mock_data = {
            "symbol": "298040",
            "name": "효성중공업",
            "sector": "전기장비",
            "industry_code": 306,
            "current_price": 2195000,
            "change_pct": -5.96,
            "per": 46.93,
            "pbr": 9.36,
            "market_cap": 204581_0000_0000,
            "peers": [
                {
                    "symbol": "267260",
                    "name": "HD현대일렉트릭",
                    "current_price": 833000,
                    "change_pct": -4.58,
                    "per": 48.68,
                    "pbr": 16.86,
                    "market_cap": 300272_6300_0000,
                },
                {
                    "symbol": "010120",
                    "name": "LS ELECTRIC",
                    "current_price": 585000,
                    "change_pct": -5.49,
                    "per": 35.0,
                    "pbr": 5.2,
                    "market_cap": 175500_0000_0000,
                },
            ],
        }
        mock_fetch = AsyncMock(return_value=mock_data)
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("298040")

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert result["symbol"] == "298040"
        assert result["name"] == "효성중공업"
        assert result["sector"] == "전기장비"
        assert len(result["peers"]) == 2
        assert result["peers"][0]["symbol"] == "267260"

        comp = result["comparison"]
        assert comp["avg_per"] is not None
        assert comp["avg_pbr"] is not None
        assert comp["target_per_rank"] is not None
        assert comp["target_pbr_rank"] is not None

    async def test_korean_equity_error_returns_payload(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(side_effect=RuntimeError("naver down"))
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("298040")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "298040"
        assert result["instrument_type"] == "equity_kr"

    async def test_us_equity_success(self, monkeypatch):
        tools = build_tools()

        # Mock Finnhub client
        class MockFinnhubClient:
            def company_peers(self, symbol):
                return ["MSFT", "GOOGL", "META"]

        _patch_runtime_attr(
            monkeypatch, "_get_finnhub_client", lambda: MockFinnhubClient()
        )

        # Mock yfinance
        _yf_data = {
            "AAPL": {
                "shortName": "Apple Inc.",
                "currentPrice": 180,
                "previousClose": 178,
                "trailingPE": 30,
                "priceToBook": 45,
                "marketCap": 3_000_000_000_000,
                "sector": "Technology",
                "industry": "Consumer Electronics",
            },
            "MSFT": {
                "shortName": "Microsoft",
                "currentPrice": 400,
                "previousClose": 398,
                "trailingPE": 35,
                "priceToBook": 12,
                "marketCap": 3_100_000_000_000,
                "sector": "Technology",
                "industry": "Software",
            },
            "GOOGL": {
                "shortName": "Alphabet",
                "currentPrice": 150,
                "previousClose": 149,
                "trailingPE": 25,
                "priceToBook": 6,
                "marketCap": 2_000_000_000_000,
                "sector": "Technology",
                "industry": "Internet",
            },
            "META": {
                "shortName": "Meta Platforms",
                "currentPrice": 500,
                "previousClose": 495,
                "trailingPE": 28,
                "priceToBook": 8,
                "marketCap": 1_300_000_000_000,
                "sector": "Technology",
                "industry": "Internet",
            },
        }

        class MockTicker:
            def __init__(self, ticker):
                self._ticker = ticker

            @property
            def info(self):
                return _yf_data.get(self._ticker, {})

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await tools["get_sector_peers"]("AAPL")

        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "finnhub+yfinance"
        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["sector"] == "Technology"
        assert len(result["peers"]) == 3
        # Sorted by market_cap desc
        assert result["peers"][0]["symbol"] == "MSFT"

        comp = result["comparison"]
        assert comp["avg_per"] is not None
        assert comp["avg_pbr"] is not None

    async def test_us_equity_error_returns_payload(self, monkeypatch):
        tools = build_tools()

        def raise_err():
            raise RuntimeError("finnhub down")

        _patch_runtime_attr(
            monkeypatch,
            "_get_finnhub_client",
            lambda: type(
                "C", (), {"company_peers": lambda self, symbol: raise_err()}
            )(),
        )

        result = await tools["get_sector_peers"]("AAPL")

        assert "error" in result
        assert result["source"] == "finnhub+yfinance"

    async def test_auto_detects_korean_market(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(
            return_value={
                "symbol": "005930",
                "name": "삼성전자",
                "sector": "반도체",
                "industry_code": 278,
                "current_price": 80000,
                "change_pct": -1.0,
                "per": 20.0,
                "pbr": 1.5,
                "market_cap": 500_0000_0000_0000,
                "peers": [],
            }
        )
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("005930")

        assert result["instrument_type"] == "equity_kr"
        mock_fetch.assert_awaited_once_with("005930", limit=5)

    async def test_limit_capped_at_20(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(
            return_value={
                "symbol": "005930",
                "name": "삼성전자",
                "sector": "반도체",
                "industry_code": 278,
                "current_price": 80000,
                "change_pct": -1.0,
                "per": 20.0,
                "pbr": 1.5,
                "market_cap": 500_0000_0000_0000,
                "peers": [],
            }
        )
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        await tools["get_sector_peers"]("005930", limit=50)

        # Should be capped at 20
        mock_fetch.assert_awaited_once_with("005930", limit=20)

    async def test_comparison_ranking_correct(self, monkeypatch):
        """Verify PER/PBR ranks are computed correctly (ascending order)."""
        tools = build_tools()

        mock_data = {
            "symbol": "298040",
            "name": "효성중공업",
            "sector": "전기장비",
            "industry_code": 306,
            "current_price": 2195000,
            "change_pct": -5.96,
            "per": 20.0,  # lowest PER
            "pbr": 5.0,  # middle PBR
            "market_cap": 200000_0000_0000,
            "peers": [
                {
                    "symbol": "A",
                    "name": "Peer A",
                    "current_price": 100000,
                    "change_pct": 1.0,
                    "per": 30.0,
                    "pbr": 3.0,  # lowest PBR
                    "market_cap": 300000_0000_0000,
                },
                {
                    "symbol": "B",
                    "name": "Peer B",
                    "current_price": 200000,
                    "change_pct": -1.0,
                    "per": 40.0,
                    "pbr": 10.0,  # highest PBR
                    "market_cap": 100000_0000_0000,
                },
            ],
        }
        monkeypatch.setattr(
            naver_finance,
            "fetch_sector_peers",
            AsyncMock(return_value=mock_data),
        )

        result = await tools["get_sector_peers"]("298040")
        comp = result["comparison"]

        # PER: target=20 is rank 1/3 (lowest = best)
        assert comp["target_per_rank"] == "1/3"
        # PBR: target=5 is rank 2/3 (middle)
        assert comp["target_pbr_rank"] == "2/3"
        # avg_per = (20+30+40)/3 = 30
        assert comp["avg_per"] == 30.0
        # avg_pbr = (5+3+10)/3 = 6.0
        assert comp["avg_pbr"] == 6.0


# ---------------------------------------------------------------------------
# simulate_avg_cost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSimulateAvgCost:
    """Tests for simulate_avg_cost tool."""

    async def test_basic_simulation_with_market_price(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 2400000, "quantity": 1},
            plans=[
                {"price": 2050000, "quantity": 1},
                {"price": 1900000, "quantity": 1},
            ],
            current_market_price=2157000,
            target_price=3080000,
        )

        # current_position
        cp = result["current_position"]
        assert cp["avg_price"] == 2400000
        assert cp["total_quantity"] == 1
        assert cp["total_invested"] == 2400000
        assert cp["unrealized_pnl"] == -243000.0
        assert cp["unrealized_pnl_pct"] == -10.12

        assert result["current_market_price"] == 2157000

        # step 1
        s1 = result["steps"][0]
        assert s1["step"] == 1
        assert s1["buy_price"] == 2050000
        assert s1["buy_quantity"] == 1
        assert s1["new_avg_price"] == 2225000
        assert s1["total_quantity"] == 2
        assert s1["total_invested"] == 4450000
        assert s1["breakeven_change_pct"] == 3.15
        assert s1["unrealized_pnl"] == -136000.0
        assert s1["unrealized_pnl_pct"] == -3.06

        # step 2
        s2 = result["steps"][1]
        assert s2["step"] == 2
        assert s2["new_avg_price"] == 2116666.67
        assert s2["total_quantity"] == 3
        assert s2["total_invested"] == 6350000
        # avg 2116666.67 / mkt 2157000 - 1 = -1.87%
        assert s2["breakeven_change_pct"] == -1.87

        # target_analysis
        ta = result["target_analysis"]
        assert ta["target_price"] == 3080000
        assert ta["final_avg_price"] == 2116666.67
        assert ta["total_return_pct"] == 45.51

    async def test_without_market_price(self):
        """Without current_market_price, P&L and breakeven fields are absent."""
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 50000, "quantity": 10},
            plans=[{"price": 40000, "quantity": 10}],
        )

        cp = result["current_position"]
        assert cp["avg_price"] == 50000
        assert "unrealized_pnl" not in cp

        s1 = result["steps"][0]
        assert s1["new_avg_price"] == 45000
        assert "breakeven_change_pct" not in s1
        assert "current_market_price" not in result
        assert "target_analysis" not in result

    async def test_with_target_only(self):
        """target_price without current_market_price still computes return."""
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 100, "quantity": 5},
            plans=[{"price": 80, "quantity": 5}],
            target_price=120,
        )

        ta = result["target_analysis"]
        assert ta["final_avg_price"] == 90
        assert ta["profit_per_unit"] == 30
        assert ta["total_profit"] == 300
        assert ta["total_return_pct"] == 33.33

    async def test_validation_missing_holdings_fields(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="holdings must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_empty_plans(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="plans must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100, "quantity": 1},
                plans=[],
            )

    async def test_validation_negative_price(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="must be >= 0"):
            await tools["simulate_avg_cost"](
                holdings={"price": -100, "quantity": 1},
                plans=[{"price": 90, "quantity": 1}],
            )

    async def test_validation_plan_missing_fields(self):
        tools = build_tools()
        with pytest.raises(ValueError, match=r"plans\[0\] must contain"):
            await tools["simulate_avg_cost"](
                holdings={"price": 100, "quantity": 1},
                plans=[{"price": 90}],
            )

    async def test_single_plan(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 1000, "quantity": 2},
            plans=[{"price": 800, "quantity": 2}],
            current_market_price=900,
        )

        assert len(result["steps"]) == 1
        s = result["steps"][0]
        assert s["new_avg_price"] == 900
        assert s["total_quantity"] == 4
        # avg == market → breakeven 0%
        assert s["breakeven_change_pct"] == 0.0
        assert s["unrealized_pnl"] == 0.0

    async def test_accepts_zero_initial_quantity_and_adds_target_metrics(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 0, "quantity": 0},
            plans=[
                {"price": 100, "quantity": 1},
                {"price": 90, "quantity": 1},
            ],
            current_market_price=95,
            target_price=120,
        )

        assert result["current_position"]["avg_price"] is None
        assert result["steps"][0]["target_return_pct"] == 20.0
        assert "pnl_vs_current" in result["steps"][0]
        assert result["steps"][1]["new_avg_price"] == 95.0
        assert result["steps"][1]["target_return_pct"] == 26.32

    async def test_requested_scenario_contains_step_target_return(self):
        tools = build_tools()
        result = await tools["simulate_avg_cost"](
            holdings={"price": 122493036, "quantity": 0.00931179},
            plans=[
                {"quantity": 0.01, "price": 100000000},
                {"quantity": 0.01, "price": 95000000},
            ],
            target_price=120000000,
            current_market_price=101692000,
        )

        assert len(result["steps"]) == 2
        for step in result["steps"]:
            assert "new_avg_price" in step
            assert "total_quantity" in step
            assert "total_invested" in step
            assert "unrealized_pnl" in step
            assert "target_return_pct" in step


# ---------------------------------------------------------------------------
# get_holdings / get_position
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_holdings_groups_by_account_and_calculates_pnl(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return [
                {
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "hldg_qty": "2",
                    "pchs_avg_pric": "70000",
                    "prpr": "70500",
                    "evlu_amt": "141000",
                    "evlu_pfls_amt": "1000",
                    "evlu_pfls_rt": "0.71",
                }
            ]

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple",
                    "ovrs_cblc_qty": "1",
                    "pchs_avg_pric": "200",
                    "now_pric2": "210",
                    "ovrs_stck_evlu_amt": "210",
                    "frcr_evlu_pfls_amt": "10",
                    "evlu_pfls_rt": "5",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {"currency": "KRW", "balance": "1000"},
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인"}}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(
            return_value=(
                [
                    {
                        "account": "toss",
                        "account_name": "기본 계좌",
                        "broker": "toss",
                        "source": "manual",
                        "instrument_type": "equity_kr",
                        "market": "kr",
                        "symbol": "005930",
                        "name": "삼성전자(토스)",
                        "quantity": 1.0,
                        "avg_buy_price": 69000.0,
                        "current_price": None,
                        "evaluation_amount": None,
                        "profit_loss": None,
                        "profit_rate": None,
                    }
                ],
                [],
            )
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_quote_equity_kr",
        AsyncMock(return_value={"price": 71000.0}),
    )
    _patch_runtime_attr(
        monkeypatch, "_fetch_quote_equity_us", AsyncMock(return_value={"price": 220.0})
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 60000000.0}),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    result = await tools["get_holdings"](minimum_value=0)

    assert result["total_accounts"] == 3
    assert result["total_positions"] == 4
    assert result["filtered_count"] == 0
    assert result["filter_reason"] == "minimum_value < 0"

    kis_account = next(item for item in result["accounts"] if item["account"] == "kis")
    kis_kr = next(
        item for item in kis_account["positions"] if item["symbol"] == "005930"
    )
    assert kis_kr["current_price"] == 71000.0
    assert kis_kr["evaluation_amount"] == 142000.0
    assert kis_kr["profit_loss"] == 2000.0
    assert kis_kr["profit_rate"] == 1.43

    upbit_account = next(
        item for item in result["accounts"] if item["account"] == "upbit"
    )
    btc = upbit_account["positions"][0]
    assert btc["symbol"] == "KRW-BTC"
    assert btc["name"] == "비트코인"
    assert btc["current_price"] == 60000000.0
    assert btc["evaluation_amount"] == 6000000.0


@pytest.mark.asyncio
async def test_get_holdings_crypto_prices_batch_fetch(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "ETH",
                    "unit_currency": "KRW",
                    "balance": "2",
                    "locked": "0",
                    "avg_buy_price": "4000000",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "ETH": "이더리움"}}
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 61000000.0, "KRW-ETH": 4200000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 61000000.0
    assert positions_by_symbol["KRW-ETH"]["current_price"] == 4200000.0
    quote_mock.assert_awaited_once()
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_get_holdings_includes_crypto_price_errors(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "DOGE",
                    "unit_currency": "KRW",
                    "balance": "100",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "DOGE": "도지"}}
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC", "KRW-DOGE"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-DOGE"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2
    assert result["filtered_count"] == 0
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 62000000.0
    assert positions_by_symbol["KRW-DOGE"]["current_price"] is None
    assert (
        positions_by_symbol["KRW-DOGE"]["price_error"]
        == "price missing in batch ticker response"
    )

    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error["source"] == "upbit"
    assert error["market"] == "crypto"
    assert error["symbol"] == "KRW-DOGE"
    assert error["stage"] == "current_price"
    assert error["error"] == "price missing in batch ticker response"
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_applies_minimum_value_filter(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "ONG",
                    "unit_currency": "KRW",
                    "balance": "1",
                    "locked": "0",
                    "avg_buy_price": "50",
                },
                {
                    "currency": "XYM",
                    "unit_currency": "KRW",
                    "balance": "0.0000007",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "0.2",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={
                "COIN_TO_NAME_KR": {
                    "BTC": "비트코인",
                    "ONG": "온톨로지가스",
                    "XYM": "심볼",
                    "PCI": "페이코인",
                }
            }
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC", "KRW-ONG", "KRW-XYM", "KRW-PCI"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ONG", "KRW-PCI", "KRW-XYM"]
        return {
            "KRW-BTC": 62000000.0,
            "KRW-ONG": 28.0,
            "KRW-XYM": 0.1,
        }

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["filtered_count"] == 2
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"
    assert result["total_positions"] == 2
    assert result["filters"]["minimum_value"] == {
        "equity_kr": 5000.0,
        "equity_us": 10.0,
        "crypto": 5000.0,
    }

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert "KRW-BTC" in positions_by_symbol
    assert "KRW-PCI" in positions_by_symbol
    assert positions_by_symbol["KRW-PCI"]["current_price"] is None
    assert (
        positions_by_symbol["KRW-PCI"]["price_error"]
        == "price missing in batch ticker response"
    )

    assert len(result["errors"]) == 1
    assert result["errors"][0]["symbol"] == "KRW-PCI"
    assert result["errors"][0]["error"] == "price missing in batch ticker response"
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_filters_delisted_markets_before_batch_fetch(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "BTC",
                    "unit_currency": "KRW",
                    "balance": "0.1",
                    "locked": "0",
                    "avg_buy_price": "50000000",
                },
                {
                    "currency": "PCI",
                    "unit_currency": "KRW",
                    "balance": "0.2",
                    "locked": "0",
                    "avg_buy_price": "100",
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "PCI": "페이코인"}}
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_current_prices",
        quote_mock,
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2
    assert result["filtered_count"] == 0
    assert result["filter_reason"] == "equity_kr < 5000, equity_us < 10, crypto < 5000"

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }
    assert positions_by_symbol["KRW-BTC"]["symbol"] == "KRW-BTC"
    assert positions_by_symbol["KRW-BTC"]["current_price"] == 62000000.0
    assert positions_by_symbol["KRW-PCI"]["current_price"] is None
    # PCI was filtered before batch fetch, so no price_error
    assert "price_error" not in positions_by_symbol["KRW-PCI"]

    assert result["errors"] == []
    quote_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_holdings_filters_account_market_and_disables_prices(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return []

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[
                {
                    "currency": "ETH",
                    "unit_currency": "KRW",
                    "balance": "1.5",
                    "locked": "0.5",
                    "avg_buy_price": "4000000",
                }
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "get_or_refresh_maps",
        AsyncMock(return_value={"COIN_TO_NAME_KR": {"ETH": "이더리움"}}),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"KRW-ETH": 4300000.0})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", quote_mock)

    result = await tools["get_holdings"](
        account="upbit", market="crypto", include_current_price=False
    )

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 1
    assert result["accounts"][0]["account"] == "upbit"

    eth = result["accounts"][0]["positions"][0]
    assert eth["symbol"] == "KRW-ETH"
    assert eth["current_price"] is None
    assert eth["evaluation_amount"] is None
    assert eth["profit_loss"] is None
    assert eth["profit_rate"] is None
    assert result["filtered_count"] == 0
    assert (
        result["filter_reason"]
        == "minimum_value filter skipped (include_current_price=False)"
    )
    quote_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_holdings_includes_top_level_summary(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": 4000000.0,
            "evaluation_amount": 4000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 33.33,
        },
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit", market="crypto", minimum_value=0
    )

    summary = result["summary"]
    assert summary["position_count"] == 2
    assert summary["total_buy_amount"] == 8000000.0
    assert summary["total_evaluation"] == 10000000.0
    assert summary["total_profit_loss"] == 2000000.0
    assert summary["total_profit_rate"] == 25.0
    assert summary["weights"][0]["symbol"] == "KRW-BTC"
    assert summary["weights"][0]["weight_pct"] == 60.0
    assert summary["weights"][1]["symbol"] == "KRW-ETH"
    assert summary["weights"][1]["weight_pct"] == 40.0


@pytest.mark.asyncio
async def test_get_holdings_summary_sets_price_dependent_fields_null(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-ETH",
            "name": "이더리움",
            "quantity": 1.0,
            "avg_buy_price": 3000000.0,
            "current_price": None,
            "evaluation_amount": None,
            "profit_loss": None,
            "profit_rate": None,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", "upbit")),
    )

    result = await tools["get_holdings"](
        account="upbit",
        market="crypto",
        include_current_price=False,
    )

    summary = result["summary"]
    assert summary["total_buy_amount"] == 3000000.0
    assert summary["total_evaluation"] is None
    assert summary["total_profit_loss"] is None
    assert summary["total_profit_rate"] is None
    assert summary["weights"] is None


@pytest.mark.asyncio
async def test_get_holdings_preserves_kis_values_on_yahoo_failure(monkeypatch):
    """Test that KIS-provided evaluation amounts are preserved when Yahoo price fetch fails."""
    tools = build_tools()

    class DummyKISClient:
        async def fetch_my_stocks(self):
            return []

        async def fetch_my_us_stocks(self):
            return [
                {
                    "ovrs_pdno": "AMZN",
                    "ovrs_item_name": "Amazon.com Inc.",
                    "ovrs_cblc_qty": "10",
                    "pchs_avg_pric": "150.0",
                    "now_pric2": "0",
                    "ovrs_stck_evlu_amt": "1600.0",
                    "frcr_evlu_pfls_amt": "100.0",
                    "evlu_pfls_rt": "6.67",
                },
                {
                    "ovrs_pdno": "AAPL",
                    "ovrs_item_name": "Apple Inc.",
                    "ovrs_cblc_qty": "5",
                    "pchs_avg_pric": "180.0",
                    "now_pric2": "0",
                    "ovrs_stck_evlu_amt": "9500.0",
                    "frcr_evlu_pfls_amt": "-500.0",
                    "evlu_pfls_rt": "-5.26",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    _patch_runtime_attr(
        monkeypatch,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    async def mock_fetch_yahoo_raise(symbol: str) -> dict[str, object]:
        raise ValueError(f"Symbol '{symbol}' not found")

    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_us", mock_fetch_yahoo_raise)

    result = await tools["get_holdings"](account="kis", market="us")

    assert result["total_accounts"] == 1
    assert result["total_positions"] == 2
    assert result["filtered_count"] == 0

    positions_by_symbol = {
        position["symbol"]: position for position in result["accounts"][0]["positions"]
    }

    amzn = positions_by_symbol["AMZN"]
    assert amzn["symbol"] == "AMZN"
    assert amzn["quantity"] == 10.0
    assert amzn["avg_buy_price"] == 150.0
    assert amzn["current_price"] is None
    assert amzn["price_error"] == "Symbol 'AMZN' not found"
    assert amzn["evaluation_amount"] == 1600.0
    assert amzn["profit_loss"] == 100.0
    assert amzn["profit_rate"] == 6.67

    aapl = positions_by_symbol["AAPL"]
    assert aapl["symbol"] == "AAPL"
    assert aapl["quantity"] == 5.0
    assert aapl["avg_buy_price"] == 180.0
    assert aapl["current_price"] is None
    assert aapl["price_error"] == "Symbol 'AAPL' not found"
    assert aapl["evaluation_amount"] == 9500.0
    assert aapl["profit_loss"] == -500.0
    assert aapl["profit_rate"] == -5.26

    assert len(result["errors"]) == 2
    error_symbols = {error["symbol"] for error in result["errors"]}
    assert "AMZN" in error_symbols
    assert "AAPL" in error_symbols
    for error in result["errors"]:
        assert error["source"] == "yahoo"
        assert error["market"] == "us"
        assert error["stage"] == "current_price"
        # Check that error message is in expected format (contains the symbol)
        assert "not found" in error["error"]


@pytest.mark.asyncio
async def test_get_position_returns_positions_and_not_holding_status(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "kis",
            "account_name": "기본 계좌",
            "broker": "kis",
            "source": "kis_api",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "삼성전자",
            "quantity": 2.0,
            "avg_buy_price": 70000.0,
            "current_price": 71000.0,
            "evaluation_amount": 142000.0,
            "profit_loss": 2000.0,
            "profit_rate": 1.43,
        },
        {
            "account": "toss",
            "account_name": "기본 계좌",
            "broker": "toss",
            "source": "manual",
            "instrument_type": "equity_kr",
            "market": "kr",
            "symbol": "005930",
            "name": "삼성전자(토스)",
            "quantity": 1.0,
            "avg_buy_price": 69000.0,
            "current_price": 71000.0,
            "evaluation_amount": 71000.0,
            "profit_loss": 2000.0,
            "profit_rate": 2.9,
        },
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        },
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "equity_kr", None)),
    )

    result = await tools["get_position"]("005930", market="kr")
    assert result["has_position"] is True
    assert result["status"] == "보유"
    assert result["position_count"] == 2
    assert sorted(result["accounts"]) == ["kis", "toss"]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "equity_us", None)),
    )
    not_holding = await tools["get_position"]("NVDA", market="us")
    assert not_holding["has_position"] is False
    assert not_holding["status"] == "미보유"


@pytest.mark.asyncio
async def test_get_position_crypto_accepts_symbol_without_prefix(monkeypatch):
    tools = build_tools()

    mocked_positions = [
        {
            "account": "upbit",
            "account_name": "기본 계좌",
            "broker": "upbit",
            "source": "upbit_api",
            "instrument_type": "crypto",
            "market": "crypto",
            "symbol": "KRW-BTC",
            "name": "비트코인",
            "quantity": 0.1,
            "avg_buy_price": 50000000.0,
            "current_price": 60000000.0,
            "evaluation_amount": 6000000.0,
            "profit_loss": 1000000.0,
            "profit_rate": 20.0,
        }
    ]

    _patch_runtime_attr(
        monkeypatch,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "crypto", None)),
    )

    result = await tools["get_position"]("BTC", market="crypto")
    assert result["has_position"] is True
    assert result["position_count"] == 1
    assert result["positions"][0]["symbol"] == "KRW-BTC"


@pytest.mark.asyncio
class TestGetCryptoProfile:
    def _reset_cache(self):
        fundamentals_sources_coingecko._COINGECKO_LIST_CACHE["expires_at"] = 0.0
        fundamentals_sources_coingecko._COINGECKO_LIST_CACHE["symbol_to_ids"] = {}
        fundamentals_sources_coingecko._COINGECKO_PROFILE_CACHE.clear()

    async def test_get_crypto_profile_success_and_cache(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        detail_calls = {"count": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/bitcoin" in url:
                    detail_calls["count"] += 1
                    return MockResponse(
                        {
                            "name": "Bitcoin",
                            "symbol": "btc",
                            "market_cap_rank": 1,
                            "categories": ["Store of Value"],
                            "description": {
                                "ko": "<p>비트코인은 대표적인 암호화폐입니다.</p>"
                            },
                            "market_data": {
                                "market_cap": {"krw": 2_000_000_000_000_000},
                                "total_volume": {"krw": 50_000_000_000_000},
                                "circulating_supply": 19_800_000,
                                "total_supply": 21_000_000,
                                "max_supply": 21_000_000,
                                "ath": {"krw": 140_000_000},
                                "ath_change_percentage": {"krw": -15.1},
                                "price_change_percentage_7d_in_currency": {"krw": 2.5},
                                "price_change_percentage_30d_in_currency": {"krw": 8.2},
                            },
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result_first = await tools["get_crypto_profile"]("KRW-BTC")
        result_second = await tools["get_crypto_profile"]("BTC")

        assert result_first["name"] == "Bitcoin"
        assert result_first["symbol"] == "BTC"
        assert result_first["market_cap"] == 2_000_000_000_000_000
        assert result_first["market_cap_rank"] == 1
        assert result_first["total_volume_24h"] == 50_000_000_000_000
        assert result_first["ath"] == 140_000_000
        assert result_first["price_change_percentage_7d"] == 2.5
        assert "<" not in (result_first["description"] or "")
        assert result_second["symbol"] == "BTC"
        assert detail_calls["count"] == 1

    async def test_get_crypto_profile_unknown_symbol_returns_error(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    return MockResponse(
                        [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_crypto_profile"]("ZZZ")

        assert "error" in result
        assert result["source"] == "coingecko"
        assert result["symbol"] == "ZZZ"


@pytest.mark.asyncio
async def test_get_support_resistance_clusters_levels(monkeypatch):
    tools = build_tools()

    base_df = pd.DataFrame(
        [
            {
                "date": "2026-02-01",
                "high": 120.0,
                "low": 80.0,
                "close": 100.0,
                "volume": 1000,
            }
        ]
    )

    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=base_df[["date", "high", "low", "close"]]),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_ohlcv_for_volume_profile",
        AsyncMock(return_value=base_df),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_calculate_fibonacci",
        lambda df, current_price: {
            "swing_high": {"price": 120.0, "date": "2026-02-01"},
            "swing_low": {"price": 80.0, "date": "2026-01-01"},
            "trend": "retracement_from_high",
            "current_price": 100.0,
            "levels": {"0.382": 110.0, "0.618": 95.0, "0.786": 89.0},
            "nearest_support": {"level": "0.618", "price": 95.0},
            "nearest_resistance": {"level": "0.382", "price": 110.0},
        },
    )
    _patch_runtime_attr(
        monkeypatch,
        "_calculate_volume_profile",
        lambda df, bins, value_area_ratio=0.70: {
            "price_range": {"low": 80.0, "high": 120.0},
            "poc": {"price": 90.0, "volume": 5000.0},
            "value_area": {"high": 111.0, "low": 89.0, "volume_pct": 70.0},
            "profile": [],
        },
    )
    _patch_runtime_attr(
        monkeypatch,
        "_compute_indicators",
        lambda df, indicators: {
            "bollinger": {"upper": 111.0, "middle": 100.0, "lower": 90.0}
        },
    )

    result = await tools["get_support_resistance"]("KRW-BTC")

    assert result["symbol"] == "KRW-BTC"
    assert result["current_price"] == 100.0
    assert result["supports"]
    assert result["resistances"]

    strong_supports = [s for s in result["supports"] if s["strength"] == "strong"]
    strong_resistances = [r for r in result["resistances"] if r["strength"] == "strong"]
    assert strong_supports
    assert strong_resistances
    assert "volume_poc" in strong_supports[0]["sources"]

    # Verify distance_pct is present and correctly calculated
    for s in result["supports"]:
        assert "distance_pct" in s
        expected = round((s["price"] - 100.0) / 100.0 * 100, 2)
        assert s["distance_pct"] == expected
        assert s["distance_pct"] < 0  # supports are below current price
    for r in result["resistances"]:
        assert "distance_pct" in r
        expected = round((r["price"] - 100.0) / 100.0 * 100, 2)
        assert r["distance_pct"] == expected
        assert r["distance_pct"] > 0  # resistances are above current price


@pytest.mark.asyncio
async def test_place_order_upbit_buy_limit_dry_run(monkeypatch):
    """Test Upbit buy limit order in dry_run mode."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols):
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
        async def fetch_multiple_current_prices(self, symbols):
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
        async def fetch_multiple_current_prices(self, symbols):
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
        async def fetch_multiple_current_prices(self, symbols):
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
        async def fetch_multiple_current_prices(self, symbols):
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


@pytest.mark.asyncio
async def test_place_order_insufficient_balance_upbit(monkeypatch):
    """Test that buying with insufficient Upbit balance shows deposit guidance."""
    tools = build_tools()

    class DummyUpbit:
        async def fetch_multiple_current_prices(self, symbols):
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
        async def inquire_domestic_cash_balance(self):
            return {"dnca_tot_amt": "100000.0", "stck_cash_ord_psbl_amt": "100000.0"}

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
        async def inquire_domestic_cash_balance(self):
            raise RuntimeError("KIS balance lookup failed")

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
    assert "KIS balance lookup failed" in result["error"]
    assert "Insufficient KRW balance: 0 KRW" not in result["error"]
    assert any("stage=balance_query" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_place_order_nyse_exchange_code(monkeypatch):
    """Test that NYSE stocks (e.g. TSM) use correct exchange code instead of hardcoded NASD."""
    tools = build_tools()

    buy_calls: list[dict] = []

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
        monkeypatch, "get_exchange_by_symbol", lambda s: "NYSE" if s == "TSM" else None
    )

    result = await tools["place_order"](
        symbol="TSM",
        side="buy",
        order_type="limit",
        quantity=10,
        price=150.0,
        dry_run=False,
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert len(buy_calls) == 1
    assert buy_calls[0]["symbol"] == "TSM"
    assert buy_calls[0]["exchange_code"] == "NYSE"
    assert buy_calls[0]["quantity"] == 10
    assert buy_calls[0]["price"] == 150.0


# ---------------------------------------------------------------------------
# DCA Plan Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeRsiWeights:
    """Tests for _compute_rsi_weights helper function."""

    def test_oversold_returns_front_heavy_weights(self):
        """RSI < 30: linear decreasing weights (more early)."""
        result = market_data_indicators._compute_rsi_weights(25.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first step gets most weight
        assert result[0] > result[1] > result[2]

    def test_oversold_with_four_splits(self):
        """RSI < 30 with splits=4."""
        result = market_data_indicators._compute_rsi_weights(28.0, 4)

        assert len(result) == 4
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first > last, monotonically decreasing
        assert result[0] > result[-1]
        assert result[0] > result[1] > result[2] > result[3]

    def test_overbought_returns_back_heavy_weights(self):
        """RSI > 50: linear increasing weights (more later)."""
        result = market_data_indicators._compute_rsi_weights(65.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Back-heavy: last step gets most weight
        assert result[2] > result[1] > result[0]

    def test_neutral_returns_equal_weights(self):
        """RSI 30-50: equal distribution."""
        result = market_data_indicators._compute_rsi_weights(40.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # All weights equal
        assert all(abs(w - result[0]) < 0.001 for w in result)

    def test_none_rsi_returns_equal_weights(self):
        """None RSI: equal distribution (same as neutral)."""
        result = market_data_indicators._compute_rsi_weights(None, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # All weights equal
        expected_weight = 1.0 / 3
        assert all(abs(w - expected_weight) < 0.001 for w in result)


@pytest.mark.unit
class TestComputeDcaPriceLevels:
    """Tests for _compute_dca_price_levels helper function."""

    def test_support_strategy_with_sufficient_supports(self):
        """Support strategy with enough support levels."""
        current_price = 100000.0
        supports = [
            {"price": 95000.0, "source": "fib_23.6"},
            {"price": 90000.0, "source": "fib_38.2"},
            {"price": 85000.0, "source": "fib_50.0"},
            {"price": 80000.0, "source": "fib_61.8"},
        ]

        result = market_data_indicators._compute_dca_price_levels(
            "support", 3, current_price, supports
        )

        assert len(result) == 3
        # Should use closest 3 supports
        assert result[0]["price"] == 95000.0
        assert result[1]["price"] == 90000.0
        assert result[2]["price"] == 85000.0

    def test_support_strategy_with_fewer_supports(self):
        """Support strategy with fewer than splits supports."""
        current_price = 100000.0
        supports = [
            {"price": 90000.0, "source": "fib_38.2"},
            {"price": 80000.0, "source": "fib_61.8"},
        ]

        result = market_data_indicators._compute_dca_price_levels(
            "support", 4, current_price, supports
        )

        assert len(result) == 4
        # Should interpolate between supports
        # First levels near supports, last levels interpolated
        assert all(level["source"] == "support" for level in result)

    def test_support_strategy_with_no_supports(self):
        """Support strategy with no support levels."""
        current_price = 100000.0
        supports = []

        result = market_data_indicators._compute_dca_price_levels(
            "support", 3, current_price, supports
        )

        assert len(result) == 3
        # Should create synthetic levels: -2%, -4%, -6%
        assert result[0]["source"] == "synthetic"
        assert abs(result[0]["price"] / current_price - 0.98) < 0.01
        assert abs(result[1]["price"] / current_price - 0.96) < 0.01
        assert abs(result[2]["price"] / current_price - 0.94) < 0.01

    def test_equal_strategy(self):
        """Equal strategy with supports."""
        current_price = 100000.0
        supports = [
            {"price": 85000.0, "source": "fib_50.0"},
        ]

        result = market_data_indicators._compute_dca_price_levels(
            "equal", 3, current_price, supports
        )

        assert len(result) == 3
        assert result[0]["source"] == "equal_spaced"
        # Should space from current_price to min support
        assert result[0]["price"] < current_price
        assert result[-1]["price"] == 85000.0

    def test_equal_strategy_without_supports(self):
        """Equal strategy without supports (goes to -10%)."""
        current_price = 100000.0
        supports = []

        result = market_data_indicators._compute_dca_price_levels(
            "equal", 3, current_price, supports
        )

        assert len(result) == 3
        assert result[-1]["price"] == 90000.0  # -10%

    def test_aggressive_strategy(self):
        """Aggressive strategy: first buy at current - 0.5%, rest support."""
        current_price = 100000.0
        supports = [
            {"price": 90000.0, "source": "fib_61.8"},
            {"price": 80000.0, "source": "fib_61.8"},
        ]

        result = market_data_indicators._compute_dca_price_levels(
            "aggressive", 3, current_price, supports
        )

        assert len(result) == 3
        assert result[0]["source"] == "aggressive_first"
        assert abs(result[0]["price"] / current_price - 0.995) < 0.001
        # Rest 2 levels from support strategy
        assert result[1]["source"] in ["support", "equal_spaced", "interpolated"]
        assert result[2]["source"] in ["support", "equal_spaced", "interpolated"]

    def test_invalid_strategy_raises_error(self):
        """Invalid strategy raises ValueError."""
        current_price = 100000.0
        supports = []

        with pytest.raises(ValueError, match="Invalid strategy"):
            market_data_indicators._compute_dca_price_levels(
                "invalid", 3, current_price, supports
            )


@pytest.mark.asyncio
class TestCreateDcaPlan:
    """Tests for create_dca_plan MCP tool."""

    async def test_create_dca_plan_market_hint_regression(self, monkeypatch):
        tools = build_tools()

        sr_calls: list[tuple[str, str | None]] = []
        indicator_calls: list[tuple[str, list[str], str | None]] = []

        async def mock_sr(symbol: str, market: str | None):
            sr_calls.append((symbol, market))
            return {
                "symbol": symbol,
                "current_price": 80000.0,
                "supports": [{"price": 76000.0, "source": "fib_23.6"}],
                "resistances": [],
            }

        async def mock_indicators(
            symbol: str, indicators: list[str], market: str | None
        ):
            indicator_calls.append((symbol, indicators, market))
            return {
                "symbol": symbol,
                "price": 80000.0,
                "indicators": {"rsi": {"14": 42.0}},
            }

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        created_plan = DcaPlan(
            id=101,
            user_id=1,
            symbol="005930",
            market="equity_kr",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        created_plan.total_amount = Decimal("900000")

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        with pytest.raises(
            ValueError, match="korean equity symbols must be 6 alphanumeric characters"
        ):
            await tools["create_dca_plan"](
                symbol="AAPL",
                market="kr",
                total_amount=900000.0,
                splits=3,
                strategy="support",
                dry_run=True,
            )

        assert sr_calls == []
        assert indicator_calls == []

        result = await tools["create_dca_plan"](
            symbol="005930",
            market="kr",
            total_amount=900000.0,
            splits=3,
            strategy="support",
            dry_run=True,
        )

        assert result["success"] is True
        assert sr_calls[-1] == ("005930", "kr")
        assert indicator_calls[-1][0] == "005930"
        assert indicator_calls[-1][1] == ["rsi"]
        assert indicator_calls[-1][2] == "kr"

    async def test_support_strategy_dry_run_crypto(self, monkeypatch):
        """Support strategy with dry_run=True for crypto."""
        tools = build_tools()

        # Mock get_support_resistance
        mock_sr_result = {
            "symbol": "KRW-BTC",
            "current_price": 100000000.0,
            "supports": [
                {"price": 95000000.0, "source": "fib_23.6"},
                {"price": 90000000.0, "source": "fib_38.2"},
            ],
            "resistances": [],
        }

        async def mock_sr(_symbol, _market):
            return mock_sr_result

        # Mock get_indicators (RSI < 30 = oversold → front_heavy)
        mock_indicator_result = {
            "symbol": "KRW-BTC",
            "price": 100000000.0,
            "indicators": {
                "rsi": {"14": 25.0},
            },
        }

        async def mock_indicators(_symbol, _indicators, _market):
            return mock_indicator_result

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        # Mock DB session and DcaService.create_plan so persistence succeeds for dry_run
        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        created_plan = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        created_plan.total_amount = Decimal("200000")

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        result = await tools["create_dca_plan"](
            symbol="KRW-BTC",
            total_amount=200000.0,
            splits=3,
            strategy="support",
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert "plans" in result
        assert "summary" in result
        assert len(result["plans"]) == 3

        # Check summary
        summary = result["summary"]
        assert summary["symbol"] == "KRW-BTC"
        assert summary["current_price"] == 100000000.0
        assert summary["rsi_14"] == 25.0
        assert summary["strategy"] == "support"
        assert summary["total_amount"] == 200000.0
        assert summary["weight_mode"] == "front_heavy"  # RSI < 30

        # Check plans
        plans = result["plans"]
        assert plans[0]["step"] == 1
        assert plans[0]["source"] in ["support", "fib_23.6"]
        assert abs(sum(p["amount"] for p in plans) - 200000.0) < 1.0

    async def test_equal_strategy_dry_run_kr_equity(self, monkeypatch):
        """Equal strategy with dry_run=True for KR equity."""
        tools = build_tools()

        mock_sr_result = {
            "symbol": "005930",
            "current_price": 80000.0,
            "supports": [
                {"price": 75000.0, "source": "fib_23.6"},
            ],
            "resistances": [],
        }

        async def mock_sr(_symbol, _market):
            return mock_sr_result

        mock_indicator_result = {
            "symbol": "005930",
            "price": 80000.0,
            "indicators": {
                "rsi": {"14": 45.0},
            },
        }

        async def mock_indicators(_symbol, _indicators, _market):
            return mock_indicator_result

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        # Mock DB session and DcaService.create_plan so persistence succeeds for dry_run
        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        created_plan = DcaPlan(
            id=2,
            user_id=1,
            symbol="005930",
            market="equity_kr",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        created_plan.total_amount = Decimal("1000000")

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        result = await tools["create_dca_plan"](
            symbol="005930",
            total_amount=1000000.0,
            splits=4,
            strategy="equal",
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert len(result["plans"]) == 4

        # KR equity uses integer quantities
        assert all(isinstance(p["quantity"], int) for p in result["plans"])
        # Equal weights: neutral RSI (30-50)
        assert result["summary"]["weight_mode"] == "equal"

    async def test_create_dca_plan_kr_valid_ticks_omit_tick_adjusted_metadata(
        self, monkeypatch
    ):
        """KR DCA plan should omit tick metadata when all prices are already valid ticks."""
        tools = build_tools()

        mock_sr_result = {
            "symbol": "012450",
            "current_price": 1_120_000.0,
            "supports": [
                {"price": 1_098_000.0, "source": "fib_23.6"},
                {"price": 1_096_000.0, "source": "fib_38.2"},
            ],
            "resistances": [],
        }

        async def mock_sr(_symbol, _market):
            return mock_sr_result

        mock_indicator_result = {
            "symbol": "012450",
            "price": 1_120_000.0,
            "indicators": {"rsi": {"14": 40.0}},
        }

        async def mock_indicators(_symbol, _indicators, _market):
            return mock_indicator_result

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        created_plan = DcaPlan(
            id=3,
            user_id=1,
            symbol="012450",
            market="equity_kr",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        created_plan.total_amount = Decimal("3000000")

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        result = await tools["create_dca_plan"](
            symbol="012450",
            total_amount=3_000_000.0,
            splits=2,
            strategy="support",
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert len(result["plans"]) == 2
        assert result["plans"][0]["price"] == 1_098_000.0
        assert result["plans"][1]["price"] == 1_096_000.0
        assert all("tick_adjusted" not in plan for plan in result["plans"])
        assert all("original_price" not in plan for plan in result["plans"])

    async def test_create_dca_plan_kr_invalid_ticks_include_tick_adjusted_metadata(
        self, monkeypatch
    ):
        """KR DCA plan should include tick metadata only when adjustment is applied."""
        tools = build_tools()

        mock_sr_result = {
            "symbol": "012450",
            "current_price": 1_120_000.0,
            "supports": [
                {"price": 1_098_500.0, "source": "fib_23.6"},
                {"price": 1_096_300.0, "source": "fib_38.2"},
            ],
            "resistances": [],
        }

        async def mock_sr(_symbol, _market):
            return mock_sr_result

        mock_indicator_result = {
            "symbol": "012450",
            "price": 1_120_000.0,
            "indicators": {"rsi": {"14": 40.0}},
        }

        async def mock_indicators(_symbol, _indicators, _market):
            return mock_indicator_result

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        created_plan = DcaPlan(
            id=4,
            user_id=1,
            symbol="012450",
            market="equity_kr",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        created_plan.total_amount = Decimal("3000000")

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        result = await tools["create_dca_plan"](
            symbol="012450",
            total_amount=3_000_000.0,
            splits=2,
            strategy="support",
            dry_run=True,
        )

        assert result["success"] is True
        assert result["dry_run"] is True
        assert len(result["plans"]) == 2
        assert result["plans"][0]["price"] == 1_098_000.0
        assert result["plans"][0]["tick_adjusted"] is True
        assert result["plans"][0]["original_price"] == 1_098_500.0
        assert result["plans"][1]["price"] == 1_096_000.0
        assert result["plans"][1]["tick_adjusted"] is True
        assert result["plans"][1]["original_price"] == 1_096_300.0

    async def test_create_dca_plan_places_order_and_marks_step_ordered(
        self, monkeypatch
    ):
        """Non-dry-run path should place order and mark first step ordered."""
        tools = build_tools()

        # Mock market resolution helpers
        mock_sr_result = {
            "symbol": "KRW-BTC",
            "current_price": 100000000.0,
            "supports": [
                {"price": 95000000.0, "source": "fib_23.6"},
                {"price": 90000000.0, "source": "fib_38.2"},
            ],
            "resistances": [],
        }

        async def mock_sr(_symbol, _market):
            return mock_sr_result

        mock_indicator_result = {
            "symbol": "KRW-BTC",
            "price": 100000000.0,
            "indicators": {
                "rsi": {"14": 40.0},
            },
        }

        async def mock_indicators(_symbol, _indicators, _market):
            return mock_indicator_result

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        # Mock DB session factory
        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Prepare a created plan with steps
        step1 = DcaPlanStep(
            id=10,
            plan_id=1,
            step_number=1,
            target_price=Decimal("95000000"),
            target_amount=Decimal("100000"),
            target_quantity=Decimal("0.001"),
            status=DcaStepStatus.PENDING,
        )
        step2 = DcaPlanStep(
            id=11,
            plan_id=1,
            step_number=2,
            target_price=Decimal("90000000"),
            target_amount=Decimal("100000"),
            target_quantity=Decimal("0.001"),
            status=DcaStepStatus.PENDING,
        )
        created_plan = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[step1, step2],
        )
        created_plan.total_amount = Decimal("100000")
        created_plan.splits = 2
        created_plan.strategy = "support"

        async def mock_create_plan(self, **kwargs):
            return created_plan

        async def mock_get_plan(self, plan_id, user_id=None):
            return created_plan

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        # Mock order placement
        async def mock_place_order_impl(*args, **kwargs):
            return {"success": True, "order_id": "ORDER-XYZ"}

        _patch_runtime_attr(monkeypatch, "_place_order_impl", mock_place_order_impl)

        # Mock mark_step_ordered
        mock_mark_step_ordered = AsyncMock()
        monkeypatch.setattr(DcaService, "mark_step_ordered", mock_mark_step_ordered)

        # Execute
        result = await tools["create_dca_plan"](
            symbol="KRW-BTC",
            total_amount=200000.0,
            splits=2,
            strategy="support",
            dry_run=False,
            execute_steps=[1],
        )

        # Verify result
        assert result["success"] is True
        assert result["dry_run"] is False
        assert result.get("executed") is True
        assert "plan_id" in result
        assert result["plan_id"] == 1
        assert "plans" in result
        assert "summary" in result

        # Verify order placement -> mark_step_ordered called with correct order_id
        mock_mark_step_ordered.assert_awaited_once()
        called_args, _ = mock_mark_step_ordered.await_args
        # called_args: (step_id, order_id) because AsyncMock is patched on the class
        assert called_args[0] == step1.id
        assert called_args[1] == "ORDER-XYZ"


@pytest.mark.asyncio
class TestCreateDcaPlanValidation:
    """Validation tests for create_dca_plan MCP tool."""

    async def test_invalid_symbol_raises_error(self, monkeypatch):
        """Empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["create_dca_plan"](
                symbol="",
                total_amount=100000.0,
                splits=3,
                dry_run=True,
            )

    async def test_invalid_splits_raises_error(self, monkeypatch):
        """splits outside 2-5 range raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="splits must be between 2 and 5"):
            await tools["create_dca_plan"](
                symbol="KRW-BTC",
                total_amount=100000.0,
                splits=6,
                dry_run=True,
            )


class TestGetDcaStatus:
    """Tests for get_dca_status MCP tool."""

    @staticmethod
    def build_tools():
        mcp = DummyMCP()
        register_all_tools(mcp)
        return mcp.tools

    @pytest.mark.asyncio
    async def test_get_dca_status_by_plan_id(self, monkeypatch):
        """Test get_dca_status by plan_id (highest priority)."""
        tools = build_tools()

        # Mock database
        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Mock DcaService.get_plan
        plan = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[
                DcaPlanStep(
                    id=1,
                    plan_id=1,
                    step_number=1,
                    status=DcaStepStatus.FILLED,
                ),
                DcaPlanStep(
                    id=2,
                    plan_id=1,
                    step_number=2,
                    status=DcaStepStatus.PENDING,
                ),
                DcaPlanStep(
                    id=3,
                    plan_id=1,
                    step_number=3,
                    status=DcaStepStatus.ORDERED,
                ),
            ],
        )

        async def mock_get_plan(self, plan_id, _user_id=None):
            if plan_id == 1:
                return plan
            return None

        monkeypatch.setattr(
            DcaService,
            "get_plan",
            mock_get_plan,
        )

        # Mock DcaService.get_plans_by_status
        async def mock_get_plans_by_status(self, **_kwargs):
            # Accept any keyword arguments
            return [plan]

        monkeypatch.setattr(
            DcaService,
            "get_plans_by_status",
            mock_get_plans_by_status,
        )

        # Execute
        result = await tools["get_dca_status"](plan_id=1)

        # Verify
        assert result["success"] is True
        assert "plans" in result
        assert len(result["plans"]) == 1
        p = result["plans"][0]
        assert p["plan_id"] == 1
        assert p["symbol"] == "KRW-BTC"
        assert p["status"] == "active"
        # Progress summary present and counts as expected
        assert "progress" in p
        assert p["progress"]["total_steps"] == 3
        assert p["progress"]["filled"] == 1
        assert p["progress"]["ordered"] == 1
        assert p["progress"]["pending"] == 1

    @pytest.mark.asyncio
    async def test_get_dca_status_by_symbol(self, monkeypatch):
        """Test get_dca_status by symbol."""
        tools = build_tools()

        plan1 = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        plan2 = DcaPlan(
            id=2,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.COMPLETED,
            steps=[],
        )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        async def mock_get_plans(self, **kwargs):
            status = kwargs.get("status")
            if status == "active":
                return [plan1]
            elif status is None:
                return [plan1, plan2]
            return []

        monkeypatch.setattr(
            DcaService,
            "get_plans_by_status",
            mock_get_plans,
        )

        # Execute
        result = await tools["get_dca_status"](symbol="KRW-BTC", status="active")

        # Verify
        assert result["success"] is True
        assert "plans" in result
        assert len(result["plans"]) == 1
        p = result["plans"][0]
        assert p["symbol"] == "KRW-BTC"
        assert p["status"] == "active"
        assert "progress" in p

    @pytest.mark.asyncio
    async def test_get_dca_status_by_status_only(self, monkeypatch):
        """Test get_dca_status by status only."""
        tools = build_tools()

        plan1 = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-ETH",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Mock DcaService.get_plans_by_status
        async def mock_get_plans(self, **kwargs):
            return [plan1]

        monkeypatch.setattr(
            DcaService,
            "get_plans_by_status",
            mock_get_plans,
        )

        # Execute
        result = await tools["get_dca_status"](status="active")

        # Verify
        assert result["success"] is True
        assert "plans" in result
        assert len(result["plans"]) == 1
        p = result["plans"][0]
        assert p["plan_id"] == 1
        assert p["symbol"] == "KRW-ETH"
        assert "progress" in p

    @pytest.mark.asyncio
    async def test_get_dca_status_all_status(self, monkeypatch):
        """Test get_dca_status with status='all'."""
        tools = build_tools()

        plan1 = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )
        plan2 = DcaPlan(
            id=2,
            user_id=1,
            symbol="KRW-ETH",
            market="crypto",
            status=DcaPlanStatus.CANCELLED,
            steps=[],
        )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Mock DcaService.get_plans_by_status
        async def mock_get_plans(self, **kwargs):
            return [plan1, plan2]

        monkeypatch.setattr(
            DcaService,
            "get_plans_by_status",
            mock_get_plans,
        )

        # Execute
        result = await tools["get_dca_status"](status="all")

        # Verify
        assert result["success"] is True
        assert "plans" in result
        assert len(result["plans"]) == 2
        assert any(
            p["status"] == "active" or p["status"] == "cancelled"
            for p in result["plans"]
        )
        for p in result["plans"]:
            assert "progress" in p

    @pytest.mark.asyncio
    async def test_get_dca_status_invalid_status(self, monkeypatch):
        """Test get_dca_status with invalid status value."""
        tools = build_tools()

        # Execute
        result = await tools["get_dca_status"](status="invalid")

        # Verify error
        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_dca_status_limit(self, monkeypatch):
        """Test get_dca_status with limit parameter."""
        tools = build_tools()

        plans_list = []
        for i in range(5):
            plans_list.append(
                DcaPlan(
                    id=i,
                    user_id=1,
                    symbol="KRW-BTC",
                    market="crypto",
                    status=DcaPlanStatus.ACTIVE,
                    steps=[],
                )
            )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Mock DcaService.get_plans_by_status
        async def mock_get_plans(self, **kwargs):
            limit = kwargs.get("limit", 10)
            return plans_list[:limit]

        monkeypatch.setattr(
            DcaService,
            "get_plans_by_status",
            mock_get_plans,
        )

        # Execute
        result = await tools["get_dca_status"](limit=3)

        # Verify
        assert result["success"] is True
        assert "plans" in result
        assert len(result["plans"]) == 3

    @pytest.mark.asyncio
    async def test_create_dca_plan_mark_step_ordered_with_reloaded_plan(
        self, monkeypatch
    ):
        """Test that mark_step_ordered is called even when created_plan.steps is empty."""
        tools = build_tools()

        # Mock DB session and plan creation
        mock_step1 = DcaPlanStep(
            id=1,
            plan_id=100,
            step_number=1,
            target_amount=Decimal("100000"),
            target_price=Decimal("100000"),
            target_quantity=Decimal("0.001"),
            status=DcaStepStatus.PENDING,
        )
        mock_step2 = DcaPlanStep(
            id=2,
            plan_id=100,
            step_number=2,
            target_amount=Decimal("100000"),
            target_price=Decimal("95000"),
            target_quantity=Decimal("0.001"),
            status=DcaStepStatus.PENDING,
        )

        mock_plan_reloaded = DcaPlan(
            id=100,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            total_amount=Decimal("100000"),
            splits=2,
            strategy="support",
            rsi_14=50.0,
            status=DcaPlanStatus.ACTIVE,
            steps=[mock_step1, mock_step2],
        )

        mock_plan_created = DcaPlan(
            id=100,
            user_id=1,
            symbol="KRW-BTC",
            market="crypto",
            total_amount=Decimal("100000"),
            splits=2,
            strategy="support",
            rsi_14=50.0,
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        # Mock DcaService.create_plan (returns plan without steps)
        async def mock_create_plan(self, **kwargs):
            return mock_plan_created

        # Mock DcaService.get_plan (returns plan with steps)
        async def mock_get_plan(self, plan_id, user_id=None):
            return mock_plan_reloaded

        monkeypatch.setattr(DcaService, "create_plan", mock_create_plan)
        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        # Mock mark_step_ordered
        mock_mark_step_ordered = AsyncMock()
        monkeypatch.setattr(DcaService, "mark_step_ordered", mock_mark_step_ordered)

        # Mock _place_order_impl to succeed for step 1
        async def mock_place_order(**kwargs):
            return {"success": True, "order_id": "ORDER-123"}

        _patch_runtime_attr(monkeypatch, "_place_order_impl", mock_place_order)

        # Mock market data
        async def mock_sr(_symbol, _market):
            return {
                "symbol": "KRW-BTC",
                "current_price": 100000.0,
                "supports": [
                    {"price": 95000.0, "source": "fib_23.6"},
                ],
                "resistances": [],
            }

        async def mock_indicators(_symbol, _indicators, _market):
            return {
                "symbol": "KRW-BTC",
                "price": 100000.0,
                "indicators": {
                    "rsi": {"14": 40.0},
                },
            }

        _patch_runtime_attr(monkeypatch, "_get_support_resistance_impl", mock_sr)
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_indicators)

        # Execute with execute_steps to trigger order placement
        result = await tools["create_dca_plan"](
            symbol="KRW-BTC",
            splits=2,
            total_amount=100000,
            strategy="support",
            dry_run=False,
            execute_steps=[1],
        )

        # Verify success and order execution
        assert result["success"] is True
        assert result["executed"] is True
        assert result["plan_id"] == 100
        assert mock_mark_step_ordered.awaited
        called_args, _ = mock_mark_step_ordered.await_args
        assert called_args[0] == 1  # step_id
        assert called_args[1] == "ORDER-123"  # order_id

    @pytest.mark.asyncio
    async def test_get_dca_status_impl_monkeypatch(self, monkeypatch):
        """Test that _get_dca_status_impl can be monkeypatched."""
        tools = build_tools()

        mock_plan = DcaPlan(
            id=1,
            user_id=1,
            symbol="KRW-ETH",
            market="crypto",
            status=DcaPlanStatus.ACTIVE,
            steps=[],
        )

        db = AsyncMock()
        _patch_runtime_attr(
            monkeypatch,
            "AsyncSessionLocal",
            lambda: DummySessionManager(db),
        )

        async def mock_get_plan(self, _plan_id, _user_id=None):
            return mock_plan

        monkeypatch.setattr(DcaService, "get_plan", mock_get_plan)

        # Monkeypatch _get_dca_status_impl
        async def mock_impl(*args, **kwargs):
            return {
                "success": True,
                "plans": [{"plan_id": 999, "symbol": "MONKEY-PATCHED"}],
                "total_plans": 1,
            }

        _patch_runtime_attr(monkeypatch, "_get_dca_status_impl", mock_impl)

        # Execute
        result = await tools["get_dca_status"](plan_id=1)

        # Verify monkeypatched implementation is called
        assert result["success"] is True
        assert result["plans"][0]["symbol"] == "MONKEY-PATCHED"


class TestPlaceOrderHighAmount:
    """Tests for place_order with high-amount orders."""

    @staticmethod
    def build_tools():
        mcp = DummyMCP()
        register_all_tools(mcp)
        return mcp.tools

    @pytest.mark.asyncio
    async def test_place_order_high_amount_kr_equity(self, monkeypatch):
        """place_order accepts high-amount orders (> 1M KRW) for KR equity."""
        tools = build_tools()

        class MockKISClient:
            async def inquire_domestic_cash_balance(self):
                return {
                    "stck_cash_ord_psbl_amt": "100000000.0",
                    "dnca_tot_amt": "100000000.0",
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
            async def inquire_domestic_cash_balance(self):
                return {
                    "stck_cash_ord_psbl_amt": "100000000.0",
                    "dnca_tot_amt": "100000000.0",
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
        _patch_runtime_attr(monkeypatch, "get_exchange_by_symbol", lambda _: "NASD")

        result = await tools["place_order"](
            symbol="AAPL",
            side="buy",
            order_type="limit",
            amount=2_500_000.0,
            price=250.0,
            dry_run=False,
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
        )

        assert result["success"] is False
        assert "Daily order limit" in result.get(
            "error", ""
        ) or "Daily order limit" in result.get("message", "")


@pytest.mark.asyncio
class TestGetInvestmentOpinions:
    """Test get_investment_opinions tool."""

    async def test_kr_symbol_int_with_leading_zeros(self, monkeypatch):
        """Test that integer symbol with leading zeros is restored for KR market."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "012450",
            "count": 1,
            "recommendations": [
                {
                    "firm": "Test Firm",
                    "rating": "buy",
                    "target_price": 50000,
                    "date": "2024-01-15",
                }
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 0,
                "sell_count": 0,
                "total_count": 1,
                "avg_target_price": 50000,
                "median_target_price": 50000,
                "min_target_price": 50000,
                "max_target_price": 50000,
                "upside_pct": 50.0,
                "current_price": 33333,
            },
        }

        async def mock_fetch(code, limit):
            return {
                "instrument_type": "equity_kr",
                "source": "naver",
                **mock_opinions,
            }

        _patch_runtime_attr(monkeypatch, "_fetch_investment_opinions_naver", mock_fetch)

        # Pass integer 12450, should be normalized to "012450"
        result = await tools["get_investment_opinions"](12450, market="kr")

        assert result["symbol"] == "012450"
        assert result["count"] == 1
        assert "consensus" in result

    async def test_kr_symbol_int_auto_detect_market(self, monkeypatch):
        """Test that integer symbol is auto-detected as KR and normalized with zfill."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "005930",
            "count": 2,
            "recommendations": [
                {
                    "firm": "Firm A",
                    "rating": "buy",
                    "target_price": 85000,
                    "date": "2024-01-15",
                },
                {
                    "firm": "Firm B",
                    "rating": "hold",
                    "target_price": 82000,
                    "date": "2024-01-14",
                },
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 1,
                "sell_count": 0,
                "total_count": 2,
                "avg_target_price": 83500,
                "current_price": 75000,
                "upside_pct": 11.33,
            },
        }

        async def mock_fetch(code, limit):
            return {
                "instrument_type": "equity_kr",
                "source": "naver",
                **mock_opinions,
            }

        _patch_runtime_attr(monkeypatch, "_fetch_investment_opinions_naver", mock_fetch)

        # Pass integer 5930, should be normalized to "005930"
        result = await tools["get_investment_opinions"](5930)

        assert result["symbol"] == "005930"

    async def test_us_market_with_only_targets(self, monkeypatch):
        """Test US market consensus calculation with only analyst_price_targets."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "AAPL",
            "count": 0,
            "recommendations": [],
            "consensus": {
                "buy_count": 0,
                "hold_count": 0,
                "sell_count": 0,
                "total_count": 0,
                "avg_target_price": 195.5,
                "median_target_price": 195.0,
                "min_target_price": 180.0,
                "max_target_price": 210.0,
                "upside_pct": 5.4,
                "current_price": 185.5,
            },
        }

        async def mock_fetch_yf(symbol, limit):
            return {
                "instrument_type": "equity_us",
                "source": "yfinance",
                **mock_opinions,
            }

        _patch_runtime_attr(
            monkeypatch, "_fetch_investment_opinions_yfinance", mock_fetch_yf
        )

        result = await tools["get_investment_opinions"]("AAPL", market="us")

        assert result["symbol"] == "AAPL"
        assert result["consensus"]["avg_target_price"] == 195.5
        assert result["consensus"]["current_price"] == 185.5
        assert result["consensus"]["upside_pct"] == 5.4

    async def test_analyze_stock_generates_recommendation_kr(self):
        """Test that _build_recommendation_for_equity generates recommendation for Korean stocks."""
        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "indicators": {
                "indicators": {
                    "rsi": {"14": 45.0},
                    "bollinger": {"lower": 74000, "middle": 75000, "upper": 76000},
                }
            },
            "support_resistance": {
                "supports": [{"price": 73000}],
                "resistances": [{"price": 77000}],
            },
            "opinions": {
                "consensus": {
                    "buy_count": 2,
                    "sell_count": 0,
                    "total_count": 2,
                    "avg_target_price": 85000,
                    "current_price": 75000,
                }
            },
        }

        # Test _build_recommendation_for_equity directly
        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_kr"
        )

        assert recommendation is not None
        assert "action" in recommendation
        assert recommendation["action"] in ("buy", "hold", "sell")
        assert "confidence" in recommendation
        assert "buy_zones" in recommendation
        assert "sell_targets" in recommendation
        assert "stop_loss" in recommendation
        assert "reasoning" in recommendation

    async def test_analyze_stock_no_recommendation_crypto(self, monkeypatch):
        """Test that analyze_stock does not generate recommendation for crypto."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "KRW-BTC",
            "market_type": "crypto",
            "source": "upbit",
            "quote": {"price": 80000000},
        }

        async def mock_impl(s, m, i):
            return mock_analysis

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", mock_impl)

        result = await tools["analyze_stock"]("KRW-BTC")

        assert "recommendation" not in result


@pytest.mark.asyncio
class TestSymbolNormalizationIntegration:
    """Test symbol normalization for all tools that accept int symbols."""

    async def test_get_quote_numeric_symbol(self, monkeypatch):
        """Test that get_quote accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_quote = {
            "symbol": "012450",
            "price": 50000,
            "instrument_type": "equity_kr",
            "source": "kis",
        }

        async def mock_fetch_quote_kr(symbol):
            return mock_quote

        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", mock_fetch_quote_kr)

        # Test with integer input
        result = await tools["get_quote"](12450, market="kr")
        assert result["symbol"] == "012450"

        # Test with string input
        result = await tools["get_quote"]("12450", market="kr")
        assert result["symbol"] == "012450"

    async def test_get_valuation_numeric_symbol(self, monkeypatch):
        """Test that get_valuation accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "012450",
            "name": "한화에어로스페이스",
            "current_price": 50000,
            "per": 15.0,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        # Test with integer input
        result = await tools["get_valuation"](12450, market="kr")
        assert result["symbol"] == "012450"

        # Test with string input
        result = await tools["get_valuation"]("12450", market="kr")
        assert result["symbol"] == "012450"

    async def test_get_news_numeric_symbol(self, monkeypatch):
        """Test that get_news accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_news = {
            "symbol": "012450",
            "count": 2,
            "news": [
                {"title": "뉴스1", "source": "연합뉴스", "datetime": "2024-01-15"},
                {"title": "뉴스2", "source": "한국경제", "datetime": "2024-01-14"},
            ],
        }

        async def mock_fetch_news(code, limit):
            return mock_news["news"]

        monkeypatch.setattr(naver_finance, "fetch_news", mock_fetch_news)

        # Test with integer input
        result = await tools["get_news"](12450, market="kr", limit=10)
        # News endpoint should normalize the symbol
        assert "012450" in str(result)

        # Test with string input
        result = await tools["get_news"]("12450", market="kr", limit=10)
        assert "012450" in str(result)


@pytest.mark.asyncio
async def test_screen_stocks_smoke(monkeypatch):
    """Smoke test for screen_stocks tool registration and basic invocation."""
    tools = build_tools()

    assert "screen_stocks" in tools

    mock_krx_stocks = [
        {
            "code": "005930",
            "name": "삼성전자",
            "close": 80000.0,
            "market": "KOSPI",
            "market_cap": 480000000000000,
        },
        {
            "code": "000660",
            "name": "SK하이닉스",
            "close": 150000.0,
            "market": "KOSPI",
            "market_cap": 15000000000000,
        },
    ]

    async def mock_fetch_stock_all_cached(market):
        return mock_krx_stocks

    async def mock_fetch_etf_all_cached():
        return []

    _patch_runtime_attr(
        monkeypatch, "fetch_stock_all_cached", mock_fetch_stock_all_cached
    )
    _patch_runtime_attr(monkeypatch, "fetch_etf_all_cached", mock_fetch_etf_all_cached)

    result = await tools["screen_stocks"](market="kr", limit=5)

    assert isinstance(result, dict)
    assert "results" in result
    assert "total_count" in result
    assert "returned_count" in result
    assert "filters_applied" in result
    assert "timestamp" in result
    assert "market" in result

    # Verify filters_applied includes required keys
    assert "market" in result["filters_applied"]
    assert "sort_by" in result["filters_applied"]
    assert "sort_order" in result["filters_applied"]

    assert isinstance(result["results"], list)


# ----------------------------------------------------------------------
# KIS 해외주식 주문/잔고 관련 테스트 (OPSQ2001 오류 방지)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kis_overseas_order_payload_fields_buy(monkeypatch):
    """해외주식 매수 주문 시 필수 필드가 올바르게 포함되는지 검증."""
    import inspect

    from app.services.kis import KISClient

    sig = inspect.signature(KISClient.order_overseas_stock)
    params = list(sig.parameters.keys())

    assert "symbol" in params
    assert "exchange_code" in params
    assert "order_type" in params
    assert "quantity" in params
    assert "price" in params


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_prefers_usd_us_row_for_orderable(
    monkeypatch,
):
    """USD 다중 행일 때 미국(natn_name) 행을 우선 사용한다."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "영국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5798.22",
                },
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5824.17",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    kis_overseas = next(
        (acc for acc in result["accounts"] if acc["account"] == "kis_overseas"),
        None,
    )
    assert kis_overseas is not None
    assert kis_overseas["balance"] == 5856.2
    assert kis_overseas["orderable"] == 5824.17


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_us_row_missing_falls_back_to_usd_max(
    monkeypatch,
):
    """미국 행이 없으면 USD 행 중 최대 일반주문가능금액을 사용한다."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "영국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5798.22",
                },
                {
                    "natn_name": "독일",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.2",
                    "frcr_gnrl_ord_psbl_amt": "5824.27",
                },
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    kis_overseas = next(
        (acc for acc in result["accounts"] if acc["account"] == "kis_overseas"),
        None,
    )
    assert kis_overseas is not None
    assert kis_overseas["balance"] == 5856.2
    assert kis_overseas["orderable"] == 5824.27


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_real_balance(monkeypatch):
    """해외 잔고 조회 시 balance/orderable이 0보다 큰 값으로 파싱되는지 테스트."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "5000000.0",
                "stck_cash_ord_psbl_amt": "4000000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5500.0",
                    "frcr_gnrl_ord_psbl_amt": "5000.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["balance"] > 0
    assert result["accounts"][0]["orderable"] > 0
    assert result["summary"]["total_usd"] > 0


@pytest.mark.asyncio
async def test_get_cash_balance_uses_new_kis_field_names(monkeypatch):
    """get_cash_balance가 새 KIS 필드명(frcr_dncl_amt1, frcr_gnrl_ord_psbl_amt)을 사용하는지 테스트."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "1000000.0",
                "stck_cash_ord_psbl_amt": "800000.0",
            }

        async def inquire_overseas_margin(self):
            return [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "3500.0",
                    "frcr_gnrl_ord_psbl_amt": "3200.0",
                }
            ]

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    result = await tools["get_cash_balance"](account="kis_overseas")

    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["balance"] == 3500.0
    assert result["accounts"][0]["orderable"] == 3200.0


@pytest.mark.asyncio
async def test_place_order_kr_limit_keeps_valid_tick_without_adjustment_metadata(
    monkeypatch,
):
    """KR limit order with valid tick price should not include tick_adjusted metadata."""
    tools = build_tools()

    class MockKISClient:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            return {"odno": "12345", "ord_qty": quantity, "ord_unpr": price}

        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "50000000",
                "stck_cash_ord_psbl_amt": "50000000",
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
    import logging

    caplog.set_level(logging.DEBUG)

    tools = build_tools()

    class MockKISClient:
        async def order_korea_stock(self, stock_code, order_type, quantity, price):
            return {"odno": "67890", "ord_qty": quantity, "ord_unpr": price}

        async def inquire_domestic_cash_balance(self):
            return {
                "dnca_tot_amt": "50000000",
                "stck_cash_ord_psbl_amt": "50000000",
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
# OPSQ2001 방지: 국내주식 주문이 통합증거금(inquire_integrated_margin)을 호출하지 않음 검증
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_place_order_kr_equity_does_not_call_integrated_margin(monkeypatch):
    """국내주식 주문이 inquire_integrated_margin을 호출하지 않고 inquire_domestic_cash_balance만 사용."""
    tools = build_tools()

    integrated_margin_called = False

    class MockKISClient:
        async def inquire_integrated_margin(self):
            nonlocal integrated_margin_called
            integrated_margin_called = True
            raise RuntimeError("OPSQ2001 should not be reached")

        async def inquire_domestic_cash_balance(self):
            return {
                "stck_cash_ord_psbl_amt": "50000000.0",
                "dnca_tot_amt": "50000000.0",
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
    assert integrated_margin_called is False


@pytest.mark.asyncio
async def test_place_order_kr_equity_opsq2001_does_not_block_order(monkeypatch):
    """inquire_integrated_margin이 OPSQ2001을 던져도 국내주식 주문은 성공해야 함 (호출되지 않으므로)."""
    tools = build_tools()

    class MockKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("OPSQ2001 CMA_EVLU_AMT_ICLD_YN error")

        async def inquire_domestic_cash_balance(self):
            return {
                "stck_cash_ord_psbl_amt": "30000000.0",
                "dnca_tot_amt": "30000000.0",
            }

    async def fetch_quote(symbol):
        return {"price": 60000.0}

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", fetch_quote)

    result = await tools["place_order"](
        symbol="005930",
        side="buy",
        order_type="market",
        amount=500_000.0,
        dry_run=True,
    )

    assert result["success"] is True
    assert result["dry_run"] is True


@pytest.mark.asyncio
async def test_place_order_kr_equity_dry_run_false_opsq2001_unaffected(monkeypatch):
    """dry_run=False에서도 inquire_integrated_margin OPSQ2001이 주문을 차단하지 않음."""
    tools = build_tools()

    order_calls: list[dict[str, object]] = []

    class MockKISClient:
        async def inquire_integrated_margin(self):
            raise RuntimeError("OPSQ2001 should not be called")

        async def inquire_domestic_cash_balance(self):
            return {
                "stck_cash_ord_psbl_amt": "100000000.0",
                "dnca_tot_amt": "100000000.0",
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
    )

    assert result["success"] is True
    assert result["dry_run"] is False
    assert len(order_calls) == 1


@pytest.mark.asyncio
async def test_recommend_stocks_registration():
    """Test recommend_stocks tool is registered."""
    tools = build_tools()
    assert "recommend_stocks" in tools
