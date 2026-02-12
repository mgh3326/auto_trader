from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from app.mcp_server import tools as mcp_tools
from app.models.dca_plan import DcaPlan, DcaPlanStatus, DcaPlanStep, DcaStepStatus
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
    mcp_tools.register_tools(mcp)
    return mcp.tools


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
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_krw_balance",
        MockUpbitService().fetch_krw_balance,
    )
    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

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
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

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
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "500.0",
                    "frcr_ord_psbl_amt": "450.0",
                }
            ]

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_krw_balance",
        MockUpbitService().fetch_krw_balance,
    )
    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", FailingKISClient)

    with pytest.raises(RuntimeError, match="KIS domestic cash balance query failed"):
        await tools["get_cash_balance"](account="kis_domestic")


@pytest.mark.asyncio
async def test_get_cash_balance_kis_overseas_fail_close(monkeypatch):
    tools = build_tools()

    class FailingKISClient:
        async def inquire_overseas_margin(self):
            raise RuntimeError("overseas margin failed")

    monkeypatch.setattr(mcp_tools, "KISClient", FailingKISClient)

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
        mcp_tools.upbit_service,
        "fetch_multiple_current_prices",
        mock.fetch_multiple_current_prices,
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_my_coins",
        mock.fetch_my_coins,
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
        mcp_tools.upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 50000000.0}),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_my_coins",
        AsyncMock(
            return_value=[{"currency": "KRW", "balance": "500000", "locked": "0"}]
        ),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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

        async def inquire_balance(self):
            return [{"crcy_cd": "KRW", "dncl_amt_2": "5000000"}]

    async def fetch_quote(symbol):
        return {"price": 100000.0}

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)
    monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_kr", fetch_quote)

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
async def test_get_open_orders_crypto(monkeypatch):
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
                    "market": "KRW-BTC",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ]

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_open_orders"]()

    assert len(result["orders"]) == 1
    assert result["orders"][0]["source"] == "upbit"
    assert result["orders"][0]["side"] == "buy"
    assert result["orders"][0]["order_id"] == "uuid-1"


@pytest.mark.asyncio
async def test_get_open_orders_kr_equity(monkeypatch):
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

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

    result = await tools["get_open_orders"]()

    assert len(result["orders"]) == 1
    assert result["orders"][0]["source"] == "kis"
    assert result["orders"][0]["market"] == "kr"
    assert result["orders"][0]["side"] == "buy"


@pytest.mark.asyncio
async def test_get_open_orders_us_equity(monkeypatch):
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

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

    result = await tools["get_open_orders"]()

    assert len(result["orders"]) == 1
    assert result["orders"][0]["source"] == "kis"
    assert result["orders"][0]["market"] == "us"
    assert result["orders"][0]["remaining_quantity"] == 50


@pytest.mark.asyncio
async def test_get_open_orders_with_symbol_filter(monkeypatch):
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
                    "market": "KRW-BTC",
                    "created_at": "2024-01-01",
                },
                {
                    "uuid": "uuid-2",
                    "side": "bid",
                    "ord_type": "limit",
                    "price": "50000.0",
                    "volume": "10.0",
                    "remaining_volume": "10.0",
                    "market": "KRW-ETH",
                    "created_at": "2024-01-01",
                },
            ]

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_open_orders"](symbol="KRW-BTC")

    assert len(result["orders"]) == 1
    assert result["orders"][0]["symbol"] == "KRW-BTC"


@pytest.mark.asyncio
async def test_get_open_orders_empty_result(monkeypatch):
    tools = build_tools()

    class MockUpbitService:
        async def fetch_open_orders(self, market):
            return []

    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )

    result = await tools["get_open_orders"]()

    assert result["total_count"] == 0
    assert len(result["orders"]) == 0


@pytest.mark.asyncio
async def test_get_open_orders_partial_failure(monkeypatch):
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
        mcp_tools.upbit_service,
        "fetch_open_orders",
        MockUpbitService().fetch_open_orders,
    )
    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

    result = await tools["get_open_orders"]()

    assert len(result["errors"]) == 1
    assert result["errors"][0]["source"] == "upbit"
    assert len(result["orders"]) == 1


@pytest.mark.asyncio
async def test_cancel_order_upbit_uuid(monkeypatch):
    tools = build_tools()
    test_uuid = "550e8400-e29b-41d4-a716-446655440000"

    class MockUpbitService:
        async def cancel_orders(self, order_uuids):
            return [{"uuid": test_uuid, "created_at": "2024-01-01T00:00:00Z"}]

    monkeypatch.setattr(
        mcp_tools.upbit_service, "cancel_orders", MockUpbitService().cancel_orders
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

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)

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
        mcp_tools.upbit_service, "cancel_orders", MockUpbitService().cancel_orders
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
    monkeypatch.setattr(
        mcp_tools,
        "get_kospi_name_to_code",
        lambda: {"삼성전자": "005930", "삼성SDI": "006400"},
    )
    monkeypatch.setattr(mcp_tools, "get_kosdaq_name_to_code", lambda: {})
    monkeypatch.setattr(
        mcp_tools,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_kospi_name_to_code",
        lambda: {"애플": "123456"},
    )
    monkeypatch.setattr(mcp_tools, "get_kosdaq_name_to_code", lambda: {})
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "get_kospi_name_to_code", raise_error)

    result = await tools["search_symbol"]("samsung")

    assert len(result) == 1
    assert result[0]["error"] == "master data failed"
    assert result[0]["source"] == "master"
    assert result[0]["query"] == "samsung"


@pytest.mark.asyncio
async def test_get_quote_crypto(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(return_value={"KRW-BTC": 123.4})
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
    )

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
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
    )

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("1234")


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
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

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
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

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
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

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
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0117V0", market="kr", count=5)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("yahoo timeout"))
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

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
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

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

    result = mcp_tools._calculate_volume_profile(df, bins=2, value_area_ratio=0.70)

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
        assert mcp_tools._normalize_market(None) is None
        assert mcp_tools._normalize_market("") is None
        assert mcp_tools._normalize_market("   ") is None

    def test_crypto_aliases(self):
        for alias in ["crypto", "upbit", "krw", "usdt"]:
            assert mcp_tools._normalize_market(alias) == "crypto"

    def test_equity_kr_aliases(self):
        for alias in ["kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr"]:
            assert mcp_tools._normalize_market(alias) == "equity_kr"

    def test_equity_us_aliases(self):
        for alias in ["us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"]:
            assert mcp_tools._normalize_market(alias) == "equity_us"

    def test_case_insensitive(self):
        assert mcp_tools._normalize_market("CRYPTO") == "crypto"
        assert mcp_tools._normalize_market("KR") == "equity_kr"
        assert mcp_tools._normalize_market("Us") == "equity_us"

    def test_unknown_returns_none(self):
        assert mcp_tools._normalize_market("unknown") is None
        assert mcp_tools._normalize_market("invalid") is None


@pytest.mark.unit
class TestSymbolDetection:
    """Tests for symbol detection helper functions."""

    def test_is_korean_equity_code(self):
        # Regular stocks (6 digits)
        assert mcp_tools._is_korean_equity_code("005930") is True
        assert mcp_tools._is_korean_equity_code("000660") is True
        assert mcp_tools._is_korean_equity_code("  005930  ") is True
        # ETF/ETN (6 alphanumeric)
        assert mcp_tools._is_korean_equity_code("0123G0") is True  # ETF
        assert mcp_tools._is_korean_equity_code("0117V0") is True  # ETF
        assert mcp_tools._is_korean_equity_code("12345A") is True  # alphanumeric
        assert mcp_tools._is_korean_equity_code("0123g0") is True  # lowercase
        # Invalid codes
        assert mcp_tools._is_korean_equity_code("00593") is False  # 5 chars
        assert mcp_tools._is_korean_equity_code("0059300") is False  # 7 chars
        assert mcp_tools._is_korean_equity_code("AAPL") is False  # 4 chars
        assert mcp_tools._is_korean_equity_code("0123-0") is False  # contains hyphen

    def test_is_crypto_market(self):
        assert mcp_tools._is_crypto_market("KRW-BTC") is True
        assert mcp_tools._is_crypto_market("krw-btc") is True
        assert mcp_tools._is_crypto_market("USDT-BTC") is True
        assert mcp_tools._is_crypto_market("usdt-eth") is True
        assert mcp_tools._is_crypto_market("BTC") is False
        assert mcp_tools._is_crypto_market("AAPL") is False
        assert mcp_tools._is_crypto_market("005930") is False

    def test_is_us_equity_symbol(self):
        assert mcp_tools._is_us_equity_symbol("AAPL") is True
        assert mcp_tools._is_us_equity_symbol("MSFT") is True
        assert mcp_tools._is_us_equity_symbol("BRK.B") is True
        assert mcp_tools._is_us_equity_symbol("KRW-BTC") is False  # crypto prefix
        assert mcp_tools._is_us_equity_symbol("005930") is False  # all digits


@pytest.mark.unit
class TestNormalizeValue:
    """Tests for _normalize_value helper function."""

    def test_none_returns_none(self):
        assert mcp_tools._normalize_value(None) is None

    def test_nan_returns_none(self):
        import numpy as np

        assert mcp_tools._normalize_value(float("nan")) is None
        assert mcp_tools._normalize_value(np.nan) is None

    def test_datetime_returns_isoformat(self):
        import datetime

        dt = datetime.datetime(2024, 1, 15, 10, 30, 0)
        assert mcp_tools._normalize_value(dt) == "2024-01-15T10:30:00"

        d = datetime.date(2024, 1, 15)
        assert mcp_tools._normalize_value(d) == "2024-01-15"

    def test_timedelta_returns_seconds(self):
        td = pd.Timedelta(hours=1, minutes=30)
        assert mcp_tools._normalize_value(td) == 5400.0

    def test_numpy_scalar_returns_python_type(self):
        import numpy as np

        assert mcp_tools._normalize_value(np.int64(42)) == 42
        assert mcp_tools._normalize_value(np.float64(3.14)) == 3.14

    def test_regular_values_pass_through(self):
        assert mcp_tools._normalize_value(42) == 42
        assert mcp_tools._normalize_value(3.14) == 3.14
        assert mcp_tools._normalize_value("hello") == "hello"


@pytest.mark.unit
class TestResolveMarketType:
    """Tests for _resolve_market_type helper function."""

    def test_explicit_crypto_normalizes_symbol(self):
        market_type, symbol = mcp_tools._resolve_market_type("krw-btc", "crypto")
        assert market_type == "crypto"
        assert symbol == "KRW-BTC"

    def test_explicit_crypto_rejects_invalid_prefix(self):
        with pytest.raises(ValueError, match="KRW-/USDT- prefix"):
            mcp_tools._resolve_market_type("BTC", "crypto")

    def test_explicit_equity_kr_validates_digits(self):
        market_type, symbol = mcp_tools._resolve_market_type("005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_explicit_equity_kr_validates_etf(self):
        """Test explicit market=kr with ETF alphanumeric code."""
        market_type, symbol = mcp_tools._resolve_market_type("0123G0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_explicit_equity_kr_validates_etf_lowercase(self):
        """Test explicit market=kr with lowercase ETF code (should be accepted)."""
        market_type, symbol = mcp_tools._resolve_market_type("0123g0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123g0"

    def test_explicit_equity_kr_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="6 alphanumeric"):
            mcp_tools._resolve_market_type("AAPL", "kr")

    def test_explicit_equity_us_rejects_crypto_prefix(self):
        with pytest.raises(ValueError, match="must not include KRW-/USDT-"):
            mcp_tools._resolve_market_type("KRW-BTC", "us")

    def test_auto_detect_crypto(self):
        market_type, symbol = mcp_tools._resolve_market_type("krw-eth", None)
        assert market_type == "crypto"
        assert symbol == "KRW-ETH"

    def test_auto_detect_korean_equity(self):
        market_type, symbol = mcp_tools._resolve_market_type("005930", None)
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_auto_detect_korean_etf(self):
        """Test auto-detection of Korean ETF code (alphanumeric)."""
        market_type, symbol = mcp_tools._resolve_market_type("0123G0", None)
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_auto_detect_korean_etf_another(self):
        """Test auto-detection with another ETF code pattern."""
        market_type, symbol = mcp_tools._resolve_market_type("0117V0", None)
        assert market_type == "equity_kr"
        assert symbol == "0117V0"

    def test_auto_detect_us_equity(self):
        market_type, symbol = mcp_tools._resolve_market_type("AAPL", None)
        assert market_type == "equity_us"
        assert symbol == "AAPL"

    def test_unsupported_symbol_raises(self):
        with pytest.raises(ValueError, match="Unsupported symbol format"):
            mcp_tools._resolve_market_type("1234", None)

    def test_market_aliases(self):
        # Test various market aliases
        assert mcp_tools._resolve_market_type("KRW-BTC", "upbit")[0] == "crypto"
        assert mcp_tools._resolve_market_type("005930", "kospi")[0] == "equity_kr"
        assert mcp_tools._resolve_market_type("AAPL", "nasdaq")[0] == "equity_us"


@pytest.mark.unit
class TestErrorPayload:
    """Tests for _error_payload helper function."""

    def test_minimal_payload(self):
        result = mcp_tools._error_payload(source="test", message="error occurred")
        assert result == {"error": "error occurred", "source": "test"}

    def test_with_symbol(self):
        result = mcp_tools._error_payload(
            source="upbit", message="not found", symbol="KRW-BTC"
        )
        assert result == {
            "error": "not found",
            "source": "upbit",
            "symbol": "KRW-BTC",
        }

    def test_with_all_fields(self):
        result = mcp_tools._error_payload(
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
        result = mcp_tools._error_payload(
            source="kis", message="error", symbol=None, instrument_type=None
        )
        assert "symbol" not in result
        assert "instrument_type" not in result


@pytest.mark.unit
class TestNormalizeRows:
    """Tests for _normalize_rows helper function."""

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        assert mcp_tools._normalize_rows(df) == []

    def test_single_row(self):
        df = pd.DataFrame([{"a": 1, "b": "text"}])
        result = mcp_tools._normalize_rows(df)
        assert result == [{"a": 1, "b": "text"}]

    def test_multiple_rows(self):
        df = pd.DataFrame([{"x": 1}, {"x": 2}, {"x": 3}])
        result = mcp_tools._normalize_rows(df)
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
        result = mcp_tools._normalize_rows(df)
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
        monkeypatch.setattr(
            mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
        )

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

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

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
        result = mcp_tools._calculate_sma(df["close"])

        assert "5" in result
        assert "20" in result
        assert "60" in result
        assert "120" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_sma(df["close"])

        assert result["5"] is not None
        assert result["20"] is None
        assert result["200"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_sma(df["close"], periods=[5, 10, 25])

        assert "5" in result
        assert "10" in result
        assert "25" in result
        assert len(result) == 3


@pytest.mark.unit
class TestCalculateEMA:
    """Tests for _calculate_ema function."""

    def test_calculates_ema_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = mcp_tools._calculate_ema(df["close"])

        assert "5" in result
        assert "20" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_ema(df["close"])

        assert result["5"] is not None
        assert result["20"] is None

    def test_ema_differs_from_sma(self):
        df = _sample_ohlcv_df(50)
        sma = mcp_tools._calculate_sma(df["close"], periods=[20])
        ema = mcp_tools._calculate_ema(df["close"], periods=[20])

        # EMA gives more weight to recent prices, so values should differ
        assert sma["20"] != ema["20"]


@pytest.mark.unit
class TestCalculateRSI:
    """Tests for _calculate_rsi function."""

    def test_calculates_rsi(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_rsi(df["close"])

        assert "14" in result
        assert result["14"] is not None
        # RSI should be between 0 and 100
        assert 0 <= result["14"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_rsi(df["close"])

        assert result["14"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_rsi(df["close"], period=7)

        assert "7" in result
        assert result["7"] is not None


@pytest.mark.unit
class TestCalculateMACD:
    """Tests for _calculate_macd function."""

    def test_calculates_macd(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_macd(df["close"])

        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(20)
        result = mcp_tools._calculate_macd(df["close"])

        assert result["macd"] is None
        assert result["signal"] is None
        assert result["histogram"] is None

    def test_histogram_equals_macd_minus_signal(self):
        df = _sample_ohlcv_df(100)
        result = mcp_tools._calculate_macd(df["close"])

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
        result = mcp_tools._calculate_bollinger(df["close"])

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
        result = mcp_tools._calculate_bollinger(df["close"])

        assert result["upper"] is None
        assert result["middle"] is None
        assert result["lower"] is None

    def test_middle_equals_sma(self):
        df = _sample_ohlcv_df(50)
        bollinger = mcp_tools._calculate_bollinger(df["close"], period=20)
        sma = mcp_tools._calculate_sma(df["close"], periods=[20])

        assert bollinger["middle"] is not None
        assert sma["20"] is not None
        assert abs(bollinger["middle"] - sma["20"]) < 0.01


@pytest.mark.unit
class TestCalculateATR:
    """Tests for _calculate_atr function."""

    def test_calculates_atr(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_atr(df["high"], df["low"], df["close"])

        assert "14" in result
        assert result["14"] is not None
        assert result["14"] > 0

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_atr(df["high"], df["low"], df["close"])

        assert result["14"] is None


@pytest.mark.unit
class TestCalculatePivot:
    """Tests for _calculate_pivot function."""

    def test_calculates_pivot_points(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

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
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

        assert result["p"] is None
        assert result["r1"] is None
        assert result["s1"] is None

    def test_pivot_ordering(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

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
class TestComputeIndicators:
    """Tests for _compute_indicators function."""

    def test_computes_single_indicator(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._compute_indicators(df, ["rsi"])

        assert "rsi" in result
        assert len(result) == 1

    def test_computes_multiple_indicators(self):
        df = _sample_ohlcv_df(100)
        result = mcp_tools._compute_indicators(df, ["sma", "ema", "rsi", "macd"])

        assert "sma" in result
        assert "ema" in result
        assert "rsi" in result
        assert "macd" in result

    def test_computes_all_indicators(self):
        df = _sample_ohlcv_df(250)
        all_indicators = ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]
        result = mcp_tools._compute_indicators(df, all_indicators)

        for indicator in all_indicators:
            assert indicator in result

    def test_raises_on_missing_columns(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            mcp_tools._compute_indicators(df, ["atr"])


@pytest.mark.asyncio
class TestGetIndicatorsTool:
    """Tests for get_indicators tool."""

    async def test_returns_indicators(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi", "macd"])

        assert result["symbol"] == "KRW-BTC"
        assert result["instrument_type"] == "crypto"
        assert result["source"] == "upbit"
        assert "price" in result
        assert "indicators" in result
        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_raises_on_empty_symbol(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_indicators"]("", ["rsi"])

    async def test_raises_on_empty_indicators(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="indicators list is required"):
            await tools["get_indicators"]("KRW-BTC", [])

    async def test_raises_on_invalid_indicator(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="Invalid indicator 'invalid'"):
            await tools["get_indicators"]("KRW-BTC", ["invalid"])

    async def test_returns_error_payload_on_failure(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(side_effect=RuntimeError("API error"))
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "error" in result
        assert result["source"] == "upbit"

    async def test_korean_equity(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await tools["get_indicators"]("005930", ["sma", "bollinger"])

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "kis"
        assert "sma" in result["indicators"]
        assert "bollinger" in result["indicators"]

    async def test_korean_etf(self, monkeypatch):
        """Test get_indicators with Korean ETF code (alphanumeric like 0123G0)."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await tools["get_indicators"]("0123G0", ["rsi", "macd"])

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "kis"
        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_us_equity(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("AAPL", ["ema", "atr", "pivot"])

        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "yahoo"
        assert "ema" in result["indicators"]
        assert "atr" in result["indicators"]
        assert "pivot" in result["indicators"]

    async def test_case_insensitive_indicators(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["RSI", "MACD", "Sma"])

        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]
        assert "sma" in result["indicators"]

    async def test_all_indicators_at_once(self, monkeypatch):
        """Test requesting all indicators in a single call."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"](
            "KRW-BTC",
            ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"],
        )

        assert "error" not in result
        assert len(result["indicators"]) == 7
        for ind in ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]:
            assert ind in result["indicators"]

    async def test_empty_dataframe_returns_error(self, monkeypatch):
        """Test that empty DataFrame returns error payload."""
        tools = build_tools()
        mock_fetch = AsyncMock(return_value=pd.DataFrame())
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "error" in result
        assert "No data available" in result["error"]

    async def test_whitespace_in_indicators(self, monkeypatch):
        """Test that whitespace in indicator names is handled."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["  rsi  ", " macd "])

        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_duplicate_indicators(self, monkeypatch):
        """Test that duplicate indicators don't cause issues."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi", "RSI", "rsi"])

        assert "error" not in result
        assert "rsi" in result["indicators"]

    async def test_price_included_in_response(self, monkeypatch):
        """Test that current price is included in response."""
        tools = build_tools()
        df = _sample_ohlcv_df(50)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "price" in result
        assert result["price"] is not None
        assert isinstance(result["price"], float)


@pytest.mark.unit
class TestFetchOhlcvForIndicators:
    """Tests for _fetch_ohlcv_for_indicators helper function."""

    @pytest.mark.asyncio
    async def test_crypto_fetch_single_batch(self, monkeypatch):
        """Test crypto fetch with count <= 200 (single batch)."""
        df = _sample_ohlcv_df(100, include_date=True)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 100)

        mock_fetch.assert_awaited_once_with(
            market="KRW-BTC", days=100, period="day", end_date=None
        )
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_crypto_pagination_multiple_batches(self, monkeypatch):
        """Test crypto fetch with count > 200 (requires pagination)."""
        import datetime

        # First batch: most recent 200 days
        df1 = _sample_ohlcv_df(200, include_date=True)
        # Second batch: next 50 days (older)
        df2 = _sample_ohlcv_df(50, include_date=True)
        # Adjust df2 dates to be older than df1
        earliest_date = df1["date"].min()
        df2["date"] = [
            earliest_date - datetime.timedelta(days=i + 1)
            for i in range(len(df2) - 1, -1, -1)
        ]

        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df1
            else:
                return df2

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 250)

        assert call_count == 2
        assert len(result) == 250  # 200 + 50

    @pytest.mark.asyncio
    async def test_crypto_pagination_handles_empty_batch(self, monkeypatch):
        """Test that pagination stops when an empty batch is returned."""
        df = _sample_ohlcv_df(100, include_date=True)
        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df
            else:
                return pd.DataFrame()  # Empty batch

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 250)

        assert call_count == 2
        assert len(result) == 100  # Only first batch

    @pytest.mark.asyncio
    async def test_crypto_pagination_removes_duplicates(self, monkeypatch):
        """Test that pagination properly removes duplicate dates."""
        df1 = _sample_ohlcv_df(100, include_date=True)
        df2 = _sample_ohlcv_df(50, include_date=True)
        # Make df2 have some overlapping dates with df1
        df2["date"] = df1["date"].iloc[:50].values

        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df1
            else:
                return df2

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 150)

        # Should have 100 unique dates (duplicates removed)
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_equity_kr_fetch(self, monkeypatch):
        df = _sample_ohlcv_df(100, include_date=True)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await mcp_tools._fetch_ohlcv_for_indicators("005930", "equity_kr", 100)

        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_equity_us_fetch(self, monkeypatch):
        df = _sample_ohlcv_df(100, include_date=True)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("AAPL", "equity_us", 100)

        mock_fetch.assert_awaited_once_with(ticker="AAPL", days=100, period="day")
        assert len(result) == 100


@pytest.mark.unit
class TestIndicatorEdgeCases:
    """Edge case tests for indicator calculations."""

    def test_sma_with_nan_values(self):
        """Test SMA handles NaN values gracefully."""
        import numpy as np

        close = pd.Series([100.0, np.nan, 102.0, 103.0, 104.0, 105.0])
        result = mcp_tools._calculate_sma(close, periods=[3])

        # pandas handles NaN in mean calculation
        assert "3" in result

    def test_ema_all_same_values(self):
        """Test EMA with constant prices."""
        close = pd.Series([100.0] * 50)
        result = mcp_tools._calculate_ema(close, periods=[20])

        # EMA of constant should equal the constant
        assert result["20"] == 100.0

    def test_rsi_strong_uptrend(self):
        """Test RSI with strong upward price movement."""
        # Create data with mostly gains and occasional small losses
        # Pattern: +2, +2, +2, -1, +2, +2, +2, -1, ... (net positive)
        prices = [100.0]
        for i in range(50):
            if (i + 1) % 4 == 0:
                prices.append(prices[-1] - 1.0)  # Small loss every 4th day
            else:
                prices.append(prices[-1] + 2.0)  # Gain most days
        close = pd.Series(prices)
        result = mcp_tools._calculate_rsi(close)

        # Strong uptrend with 3:1 gain ratio should have high RSI
        assert result["14"] is not None
        assert result["14"] > 70

    def test_rsi_all_losses(self):
        """Test RSI with only downward price movement."""
        close = pd.Series(range(100, 1, -1))  # 100, 99, 98, ... 2
        result = mcp_tools._calculate_rsi(close.astype(float))

        # All losses, no gains -> RSI should be 0
        assert result["14"] == 0.0

    def test_macd_with_trend(self):
        """Test MACD detects upward trend."""
        # Create upward trending data
        close = pd.Series([100 + i * 0.5 for i in range(100)])
        result = mcp_tools._calculate_macd(close)

        # In uptrend, fast EMA > slow EMA, so MACD should be positive
        assert result["macd"] is not None
        assert result["macd"] > 0

    def test_bollinger_width_increases_with_volatility(self):
        """Test Bollinger band width reflects volatility."""
        # Low volatility
        close_low_vol = pd.Series([100.0 + (i % 2) * 0.1 for i in range(50)])
        result_low = mcp_tools._calculate_bollinger(close_low_vol)

        # High volatility
        close_high_vol = pd.Series([100.0 + (i % 2) * 5.0 for i in range(50)])
        result_high = mcp_tools._calculate_bollinger(close_high_vol)

        assert result_low["upper"] is not None
        assert result_low["lower"] is not None
        assert result_high["upper"] is not None
        assert result_high["lower"] is not None
        low_width = result_low["upper"] - result_low["lower"]
        high_width = result_high["upper"] - result_high["lower"]

        assert high_width > low_width

    def test_atr_reflects_volatility(self):
        """Test ATR increases with larger price ranges."""
        # Low volatility
        df_low = pd.DataFrame(
            {
                "high": [101.0] * 50,
                "low": [99.0] * 50,
                "close": [100.0] * 50,
            }
        )
        result_low = mcp_tools._calculate_atr(
            df_low["high"], df_low["low"], df_low["close"]
        )

        # High volatility
        df_high = pd.DataFrame(
            {
                "high": [110.0] * 50,
                "low": [90.0] * 50,
                "close": [100.0] * 50,
            }
        )
        result_high = mcp_tools._calculate_atr(
            df_high["high"], df_high["low"], df_high["close"]
        )

        assert result_high["14"] is not None
        assert result_low["14"] is not None
        assert result_high["14"] > result_low["14"]

    def test_pivot_formula_verification(self):
        """Verify pivot point formula correctness."""
        high = pd.Series([110.0, 115.0])
        low = pd.Series([90.0, 95.0])
        close = pd.Series([100.0, 105.0])

        result = mcp_tools._calculate_pivot(high, low, close)

        # Using previous day's data (index -2): H=110, L=90, C=100
        expected_p = (110 + 90 + 100) / 3  # 100
        expected_r1 = 2 * expected_p - 90  # 110
        expected_s1 = 2 * expected_p - 110  # 90

        assert abs(result["p"] - expected_p) < 0.01
        assert abs(result["r1"] - expected_r1) < 0.01
        assert abs(result["s1"] - expected_s1) < 0.01

    def test_compute_indicators_with_minimal_data(self):
        """Test compute_indicators with just enough data."""
        df = _sample_ohlcv_df(5)
        result = mcp_tools._compute_indicators(df, ["sma", "rsi", "macd"])

        # SMA(5) should work, but RSI(14) and MACD should return None
        assert result["sma"]["5"] is not None
        assert result["rsi"]["14"] is None
        assert result["macd"]["macd"] is None


@pytest.mark.unit
class TestIndicatorDefaultConstants:
    """Test that default constants are properly defined."""

    def test_default_sma_periods(self):
        assert mcp_tools.DEFAULT_SMA_PERIODS == [5, 20, 60, 120, 200]

    def test_default_ema_periods(self):
        assert mcp_tools.DEFAULT_EMA_PERIODS == [5, 20, 60, 120, 200]

    def test_default_rsi_period(self):
        assert mcp_tools.DEFAULT_RSI_PERIOD == 14

    def test_default_macd_params(self):
        assert mcp_tools.DEFAULT_MACD_FAST == 12
        assert mcp_tools.DEFAULT_MACD_SLOW == 26
        assert mcp_tools.DEFAULT_MACD_SIGNAL == 9

    def test_default_bollinger_params(self):
        assert mcp_tools.DEFAULT_BOLLINGER_PERIOD == 20
        assert mcp_tools.DEFAULT_BOLLINGER_STD == 2.0

    def test_default_atr_period(self):
        assert mcp_tools.DEFAULT_ATR_PERIOD == 14


# ---------------------------------------------------------------------------
# Finnhub Tools Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetFinnhubClient:
    """Test Finnhub client initialization."""

    def test_missing_api_key_raises_error(self, monkeypatch):
        """Test that missing API key raises ValueError."""
        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", None)

        with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
            mcp_tools._get_finnhub_client()

    def test_returns_client_with_valid_key(self, monkeypatch):
        """Test that valid API key returns client."""
        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")

        client = mcp_tools._get_finnhub_client()

        assert client is not None


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetNews:
    """Test get_news tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_news"]("")

    async def test_invalid_market_raises_error(self):
        """Test that invalid market raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="market must be"):
            await tools["get_news"]("AAPL", market="invalid")

    async def test_us_news_success(self, monkeypatch):
        """Test successful US news fetch."""
        tools = build_tools()

        mock_news = [
            {
                "headline": "Apple announces new product",
                "source": "Bloomberg",
                "datetime": 1704067200,  # 2024-01-01
                "url": "https://example.com/news",
                "summary": "Apple released...",
                "sentiment": 0.5,
                "related": "AAPL",
            }
        ]

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                return mock_news

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us", limit=5)

        assert result["symbol"] == "AAPL"
        assert result["market"] == "us"
        assert result["source"] == "finnhub"
        assert result["count"] == 1
        assert result["news"][0]["title"] == "Apple announces new product"

    async def test_crypto_news_success(self, monkeypatch):
        """Test successful crypto news fetch."""
        tools = build_tools()

        mock_news = [
            {
                "headline": "Bitcoin reaches new high",
                "source": "CoinDesk",
                "datetime": 1704067200,
                "url": "https://example.com/crypto",
                "summary": "BTC surged...",
            }
        ]

        class MockClient:
            def __init__(self, api_key):
                pass

            def general_news(self, category, min_id):
                return mock_news

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("BTC", market="crypto", limit=5)

        assert result["market"] == "crypto"
        assert result["news"][0]["title"] == "Bitcoin reaches new high"

    async def test_returns_error_payload_on_failure(self, monkeypatch):
        """Test that API errors return error payload."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                raise RuntimeError("API error")

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us")

        assert "error" in result
        assert result["source"] == "finnhub"

    async def test_limit_capped_at_50(self, monkeypatch):
        """Test that limit is capped at 50."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                return [{"headline": f"News {i}"} for i in range(100)]

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us", limit=100)

        assert result["count"] <= 50


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetCompanyProfile:
    """Test get_company_profile tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_company_profile"]("")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_company_profile"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful company profile fetch."""
        tools = build_tools()

        mock_profile = {
            "name": "Apple Inc",
            "ticker": "AAPL",
            "country": "US",
            "currency": "USD",
            "exchange": "NASDAQ",
            "ipo": "1980-12-12",
            "marketCapitalization": 3000000,
            "shareOutstanding": 15000,
            "finnhubIndustry": "Technology",
            "weburl": "https://apple.com",
            "logo": "https://example.com/logo.png",
            "phone": "1234567890",
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return mock_profile

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_company_profile"]("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc"
        assert result["sector"] == "Technology"
        assert result["market_cap"] == 3000000

    async def test_not_found_returns_error(self, monkeypatch):
        """Test that not found returns error payload."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return {}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_company_profile"]("INVALID")

        assert "error" in result


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFinancials:
    """Test get_financials tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_financials"]("")

    async def test_invalid_statement_raises_error(self):
        """Test that invalid statement type raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="statement must be"):
            await tools["get_financials"]("AAPL", statement="invalid")

    async def test_invalid_freq_raises_error(self):
        """Test that invalid frequency raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="freq must be"):
            await tools["get_financials"]("AAPL", freq="invalid")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_financials"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful financials fetch."""
        tools = build_tools()

        mock_data = {
            "data": [
                {
                    "year": 2024,
                    "quarter": 0,
                    "filedDate": "2024-01-15",
                    "startDate": "2023-01-01",
                    "endDate": "2023-12-31",
                    "report": {
                        "ic": [
                            {"label": "Revenue", "value": 100000000},
                            {"label": "Net Income", "value": 20000000},
                        ]
                    },
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def financials_reported(self, symbol, freq):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_financials"](
            "AAPL", statement="income", freq="annual"
        )

        assert result["symbol"] == "AAPL"
        assert result["statement"] == "income"
        assert result["freq"] == "annual"
        assert len(result["reports"]) == 1
        assert result["reports"][0]["data"]["Revenue"] == 100000000


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInsiderTransactions:
    """Test get_insider_transactions tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_insider_transactions"]("")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_insider_transactions"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful insider transactions fetch."""
        tools = build_tools()

        mock_data = {
            "data": [
                {
                    "name": "Tim Cook",
                    "transactionCode": "S",
                    "share": 50000,
                    "change": -50000,
                    "transactionPrice": 180.0,
                    "transactionDate": "2024-01-15",
                    "filingDate": "2024-01-17",
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=10)

        assert result["symbol"] == "AAPL"
        assert result["count"] == 1
        assert result["transactions"][0]["name"] == "Tim Cook"
        assert result["transactions"][0]["shares"] == 50000
        assert result["transactions"][0]["transaction_type"] == "Sale"
        assert result["transactions"][0]["transaction_code"] == "S"
        assert result["transactions"][0]["change"] == -50000

    async def test_limit_capped_at_100(self, monkeypatch):
        """Test that limit is capped at 100."""
        tools = build_tools()

        mock_data = {"data": [{"name": f"Exec {i}"} for i in range(150)]}

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=200)

        assert result["count"] <= 100

    async def test_transaction_code_mapping(self, monkeypatch):
        """Test that transaction codes are properly mapped to readable types."""
        tools = build_tools()

        mock_data = {
            "data": [
                {"name": "Exec 1", "transactionCode": "P", "share": 1000},
                {"name": "Exec 2", "transactionCode": "A", "share": 500},
                {"name": "Exec 3", "transactionCode": "M", "share": 200},
                {
                    "name": "Exec 4",
                    "transactionCode": "X",
                    "share": 100,
                },  # Unknown code
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=10)

        assert result["transactions"][0]["transaction_type"] == "Purchase"
        assert result["transactions"][0]["transaction_code"] == "P"
        assert result["transactions"][1]["transaction_type"] == "Grant/Award"
        assert result["transactions"][1]["transaction_code"] == "A"
        assert result["transactions"][2]["transaction_type"] == "Option Exercise"
        assert result["transactions"][2]["transaction_code"] == "M"
        # Unknown code should fall back to the code itself
        assert result["transactions"][3]["transaction_type"] == "X"
        assert result["transactions"][3]["transaction_code"] == "X"

    async def test_empty_transactions(self, monkeypatch):
        """Test handling of empty insider transactions."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return {"data": []}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("UNKNOWN")

        assert result["count"] == 0
        assert result["transactions"] == []

    async def test_no_data_in_response(self, monkeypatch):
        """Test handling when API returns no data field."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return {}  # No "data" field

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL")

        assert result["count"] == 0
        assert result["transactions"] == []


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetEarningsCalendar:
    """Test get_earnings_calendar tool."""

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_earnings_calendar"](symbol="KRW-BTC")

    async def test_korean_symbol_raises_error(self):
        """Test that Korean symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_earnings_calendar"](symbol="005930")

    async def test_invalid_date_format_raises_error(self):
        """Test that invalid date format raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="ISO format"):
            await tools["get_earnings_calendar"](from_date="invalid")

    async def test_success_with_symbol(self, monkeypatch):
        """Test successful earnings calendar fetch with symbol."""
        tools = build_tools()

        mock_data = {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2024-01-25",
                    "hour": "amc",
                    "epsEstimate": 2.10,
                    "epsActual": 2.18,
                    "revenueEstimate": 118000000000,
                    "revenueActual": 119600000000,
                    "quarter": 1,
                    "year": 2024,
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](symbol="AAPL")

        assert result["symbol"] == "AAPL"
        assert result["count"] == 1
        assert result["earnings"][0]["eps_estimate"] == 2.10
        assert result["earnings"][0]["eps_actual"] == 2.18

    async def test_success_without_symbol(self, monkeypatch):
        """Test successful earnings calendar fetch without symbol (date range only)."""
        tools = build_tools()

        mock_data = {
            "earningsCalendar": [
                {"symbol": "AAPL", "date": "2024-01-25"},
                {"symbol": "MSFT", "date": "2024-01-30"},
            ]
        }

        captured_symbol = None

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                nonlocal captured_symbol
                captured_symbol = symbol
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](
            from_date="2024-01-01", to_date="2024-01-31"
        )

        assert result["count"] == 2
        # Verify empty string is passed when symbol is None
        assert captured_symbol == ""

    async def test_default_dates_when_not_provided(self, monkeypatch):
        """Test that default dates are set when not provided."""
        tools = build_tools()

        mock_data = {"earningsCalendar": []}
        captured_from = None
        captured_to = None

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                nonlocal captured_from, captured_to
                captured_from = _from
                captured_to = to
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"]()

        # Verify dates are set (today and 30 days from now)
        assert captured_from is not None
        assert captured_to is not None
        assert result["from_date"] == captured_from
        assert result["to_date"] == captured_to

    async def test_empty_result(self, monkeypatch):
        """Test handling of empty earnings calendar."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                return {"earningsCalendar": []}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](symbol="UNKNOWN")

        assert result["count"] == 0
        assert result["earnings"] == []


# ---------------------------------------------------------------------------
# Naver Finance (Korean Market) Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetNewsKorea:
    """Test get_news tool with Korean market."""

    async def test_korean_stock_news(self, monkeypatch):
        """Test fetching news for Korean stock."""
        tools = build_tools()

        mock_news = [
            {
                "title": "삼성전자, 신제품 발표",
                "source": "연합뉴스",
                "datetime": "2024-01-15",
                "url": "https://finance.naver.com/news/1",
            },
        ]

        async def mock_fetch_news(code, limit):
            return mock_news

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        result = await tools["get_news"]("005930", market="kr")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["market"] == "kr"
        assert len(result["news"]) == 1
        assert result["news"][0]["title"] == "삼성전자, 신제품 발표"

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_news(code, limit):
            return []

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        # Should auto-detect Korean market from 6-digit code
        result = await tools["get_news"]("005930")

        assert result["source"] == "naver"
        assert result["market"] == "kr"

    async def test_error_handling(self, monkeypatch):
        """Test error handling for Korean news fetch."""
        tools = build_tools()

        async def mock_fetch_news(code, limit):
            raise ValueError("Network error")

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        result = await tools["get_news"]("005930", market="kr")

        assert "error" in result
        assert result["source"] == "naver"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetCompanyProfileKorea:
    """Test get_company_profile tool with Korean market."""

    async def test_korean_stock_profile(self, monkeypatch):
        """Test fetching company profile for Korean stock."""
        tools = build_tools()

        mock_profile = {
            "symbol": "005930",
            "name": "삼성전자",
            "exchange": "KOSPI",
            "sector": "전기전자",
            "market_cap": 400_0000_0000_0000,
            "per": 15.23,
            "pbr": 1.45,
        }

        async def mock_fetch_profile(code):
            return mock_profile

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_company_profile", mock_fetch_profile
        )

        result = await tools["get_company_profile"]("005930")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"
        assert result["name"] == "삼성전자"

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_profile(code):
            return {"symbol": code, "name": "테스트"}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_company_profile", mock_fetch_profile
        )

        result = await tools["get_company_profile"]("005930")

        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"

    async def test_explicit_us_market_for_korean_looking_symbol(self, monkeypatch):
        """Test explicit US market override."""
        tools = build_tools()

        mock_profile = {
            "name": "US Company",
            "ticker": "123456",
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return mock_profile

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        # Even though 123456 looks like Korean code, explicit market=us should use Finnhub
        result = await tools["get_company_profile"]("123456", market="us")

        assert result["source"] == "finnhub"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFinancialsKorea:
    """Test get_financials tool with Korean market."""

    async def test_korean_stock_financials(self, monkeypatch):
        """Test fetching financials for Korean stock."""
        tools = build_tools()

        mock_financials = {
            "symbol": "005930",
            "statement": "income",
            "freq": "annual",
            "currency": "KRW",
            "periods": ["2023/12", "2022/12"],
            "metrics": {
                "매출액": [300_0000_0000_0000, 280_0000_0000_0000],
                "영업이익": [50_0000_0000_0000, 45_0000_0000_0000],
            },
        }

        async def mock_fetch_financials(code, statement, freq):
            return mock_financials

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_financials", mock_fetch_financials
        )

        result = await tools["get_financials"]("005930", statement="income")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"
        assert "매출액" in result["metrics"]

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_financials(code, statement, freq):
            return {"symbol": code, "statement": statement, "freq": freq}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_financials", mock_fetch_financials
        )

        result = await tools["get_financials"]("005930")

        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInvestorTrends:
    """Test get_investor_trends tool."""

    async def test_success(self, monkeypatch):
        """Test successful investor trends fetch."""
        tools = build_tools()

        mock_trends = {
            "symbol": "005930",
            "days": 20,
            "data": [
                {
                    "date": "2024-01-15",
                    "close": 75000,
                    "change": 500,
                    "volume": 10000000,
                    "foreign_net": -500000,
                    "institutional_net": 1000000,
                },
                {
                    "date": "2024-01-14",
                    "close": 74500,
                    "change": -300,
                    "volume": 8000000,
                    "foreign_net": 300000,
                    "institutional_net": -200000,
                },
            ],
        }

        async def mock_fetch_trends(code, days):
            return mock_trends

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investor_trends", mock_fetch_trends
        )

        result = await tools["get_investor_trends"]("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert len(result["data"]) == 2
        assert result["data"][0]["foreign_net"] == -500000

    async def test_rejects_us_symbol(self):
        """Test that US symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investor_trends"]("AAPL")

    async def test_rejects_crypto_symbol(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investor_trends"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_investor_trends"]("")

    async def test_days_capped(self, monkeypatch):
        """Test that days are capped at 60."""
        tools = build_tools()

        captured_days = None

        async def mock_fetch_trends(code, days):
            nonlocal captured_days
            captured_days = days
            return {"symbol": code, "days": days, "data": []}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investor_trends", mock_fetch_trends
        )

        await tools["get_investor_trends"]("005930", days=100)

        assert captured_days == 60


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInvestmentOpinions:
    """Test get_investment_opinions tool."""

    async def test_success(self, monkeypatch):
        """Test successful investment opinions fetch."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "005930",
            "count": 2,
            "opinions": [
                {
                    "stock_name": "삼성전자",
                    "title": "반도체 업황 개선 전망",
                    "firm": "삼성증권",
                    "rating": "매수",
                    "target_price": 85000,
                    "date": "2024-01-15",
                },
                {
                    "stock_name": "삼성전자",
                    "title": "실적 호조 지속",
                    "firm": "미래에셋",
                    "rating": "Strong Buy",
                    "target_price": 90000,
                    "date": "2024-01-14",
                },
            ],
            "current_price": 75000,
            "avg_target_price": 87500,
            "max_target_price": 90000,
            "min_target_price": 85000,
            "upside_potential": 16.67,
        }

        async def mock_fetch_opinions(code, limit):
            return mock_opinions

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investment_opinions", mock_fetch_opinions
        )

        result = await tools["get_investment_opinions"]("005930", limit=10)

        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert result["count"] == 2
        assert result["opinions"][0]["firm"] == "삼성증권"
        # Check new target price statistics
        assert result["current_price"] == 75000
        assert result["avg_target_price"] == 87500
        assert result["max_target_price"] == 90000
        assert result["min_target_price"] == 85000
        assert result["upside_potential"] == 16.67

    async def test_successful_us_opinions_fetch(self, monkeypatch):
        """Test successful investment opinions fetch for US stock via yfinance."""
        tools = build_tools()

        mock_targets = {
            "current": 185.5,
            "high": 250.0,
            "low": 180.0,
            "mean": 210.5,
            "median": 212.0,
        }

        mock_ud = pd.DataFrame(
            [
                {
                    "GradeDate": pd.Timestamp("2025-01-15"),
                    "Firm": "Morgan Stanley",
                    "ToGrade": "Overweight",
                    "FromGrade": "Equal-Weight",
                    "Action": "up",
                    "currentPriceTarget": 230.0,
                    "priorPriceTarget": 200.0,
                },
                {
                    "GradeDate": pd.Timestamp("2025-01-10"),
                    "Firm": "Goldman Sachs",
                    "ToGrade": "Buy",
                    "FromGrade": "Buy",
                    "Action": "main",
                    "currentPriceTarget": 220.0,
                    "priorPriceTarget": 210.0,
                },
            ]
        ).set_index("GradeDate")

        mock_info = {"currentPrice": 185.5}

        class MockTicker:
            @property
            def analyst_price_targets(self):
                return mock_targets

            @property
            def upgrades_downgrades(self):
                return mock_ud

            @property
            def info(self):
                return mock_info

        monkeypatch.setattr("app.mcp_server.tools.yf.Ticker", lambda s: MockTicker())

        result = await tools["get_investment_opinions"]("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "yfinance"
        assert result["current_price"] == 185.5
        assert result["avg_target_price"] == 210.5
        assert result["max_target_price"] == 250.0
        assert result["min_target_price"] == 180.0
        assert result["upside_potential"] == 13.48
        assert result["count"] == 2
        assert result["recommendations"][0]["firm"] == "Morgan Stanley"
        assert result["recommendations"][0]["rating"] == "Overweight"
        assert result["recommendations"][0]["date"] == "2025-01-15"
        assert result["recommendations"][0]["target_price"] == 230.0

    async def test_us_opinions_error_handling(self, monkeypatch):
        """Test error handling when yfinance fetch fails."""
        tools = build_tools()

        class MockTicker:
            @property
            def analyst_price_targets(self):
                raise Exception("API error")

            @property
            def upgrades_downgrades(self):
                raise Exception("API error")

            @property
            def info(self):
                raise Exception("API error")

        monkeypatch.setattr("app.mcp_server.tools.yf.Ticker", lambda s: MockTicker())

        # Should not raise — errors are caught gracefully and return partial data
        result = await tools["get_investment_opinions"]("AAPL")
        assert result["symbol"] == "AAPL"
        assert result["instrument_type"] == "equity_us"

    async def test_rejects_crypto_symbol(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="cryptocurrencies"):
            await tools["get_investment_opinions"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_investment_opinions"]("")

    async def test_limit_capped(self, monkeypatch):
        """Test that limit is capped at 30."""
        tools = build_tools()

        captured_limit = None

        async def mock_fetch_opinions(code, limit):
            nonlocal captured_limit
            captured_limit = limit
            return {
                "symbol": code,
                "count": 0,
                "opinions": [],
                "current_price": None,
                "avg_target_price": None,
                "max_target_price": None,
                "min_target_price": None,
                "upside_potential": None,
            }

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investment_opinions", mock_fetch_opinions
        )

        await tools["get_investment_opinions"]("005930", limit=100)

        assert captured_limit == 30

    async def test_invalid_market_raises_error(self):
        """Test that invalid market raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="must be 'us' or 'kr'"):
            await tools["get_investment_opinions"]("AAPL", market="invalid")


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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

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

        monkeypatch.setattr("app.mcp_server.tools.yf.Ticker", lambda s: MockTicker())

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

        monkeypatch.setattr("app.mcp_server.tools.yf.Ticker", lambda s: MockTicker())

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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

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

        monkeypatch.setattr("app.mcp_server.tools.yf.Ticker", lambda s: MockTicker())

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
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
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
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        await tools["get_short_interest"]("005930", days=100)

        assert captured_days == 60

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_short_interest(code, days):
            raise Exception("KRX API error")

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
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
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
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
            mcp_tools.upbit_service,
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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

        monkeypatch.setattr(
            mcp_tools,
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
            mcp_tools.upbit_service,
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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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
        assert mcp_tools._parse_naver_num(None) is None
        assert mcp_tools._parse_naver_int(None) is None

    def test_numeric(self):
        assert mcp_tools._parse_naver_num(1234.5) == 1234.5
        assert mcp_tools._parse_naver_num(100) == 100.0
        assert mcp_tools._parse_naver_int(42) == 42

    def test_string_with_commas(self):
        assert mcp_tools._parse_naver_num("2,450.50") == 2450.50
        assert mcp_tools._parse_naver_num("-45.30") == -45.30
        assert mcp_tools._parse_naver_int("450,000,000") == 450000000

    def test_invalid_string(self):
        assert mcp_tools._parse_naver_num("abc") is None
        assert mcp_tools._parse_naver_int("abc") is None


@pytest.mark.unit
class TestIndexMeta:
    """Tests for _INDEX_META and _DEFAULT_INDICES."""

    def test_all_default_indices_have_meta(self):
        for sym in mcp_tools._DEFAULT_INDICES:
            assert sym in mcp_tools._INDEX_META

    def test_korean_indices_have_naver_code(self):
        for sym in ("KOSPI", "KOSDAQ"):
            meta = mcp_tools._INDEX_META[sym]
            assert meta["source"] == "naver"
            assert "naver_code" in meta

    def test_us_indices_have_yf_ticker(self):
        for sym in ("SPX", "NASDAQ", "DJI"):
            meta = mcp_tools._INDEX_META[sym]
            assert meta["source"] == "yfinance"
            assert "yf_ticker" in meta

    def test_aliases(self):
        assert (
            mcp_tools._INDEX_META["SPX"]["yf_ticker"]
            == mcp_tools._INDEX_META["SP500"]["yf_ticker"]
        )
        assert (
            mcp_tools._INDEX_META["DJI"]["yf_ticker"]
            == mcp_tools._INDEX_META["DOW"]["yf_ticker"]
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
        result = mcp_tools._calculate_fibonacci(df, current_price)

        assert result["trend"] == "retracement_from_high"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing high, 100% level = swing low
        assert result["levels"]["0.0"] > result["levels"]["1.0"]

    def test_downtrend_bounce_from_low(self):
        df = _fib_df_downtrend()
        current_price = float(df["close"].iloc[-1])
        result = mcp_tools._calculate_fibonacci(df, current_price)

        assert result["trend"] == "bounce_from_low"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing low, 100% level = swing high
        assert result["levels"]["0.0"] < result["levels"]["1.0"]

    def test_all_seven_levels_present(self):
        df = _fib_df_uptrend()
        result = mcp_tools._calculate_fibonacci(df, 150.0)

        expected_keys = {"0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0"}
        assert set(result["levels"].keys()) == expected_keys

    def test_nearest_support_and_resistance(self):
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        swing_low = float(df["low"].min())
        mid = (swing_high + swing_low) / 2
        result = mcp_tools._calculate_fibonacci(df, mid)

        if result["nearest_support"] is not None:
            assert result["nearest_support"]["price"] < mid
        if result["nearest_resistance"] is not None:
            assert result["nearest_resistance"]["price"] > mid

    def test_dates_are_strings(self):
        df = _fib_df_uptrend()
        result = mcp_tools._calculate_fibonacci(df, 150.0)

        assert isinstance(result["swing_high"]["date"], str)
        assert isinstance(result["swing_low"]["date"], str)
        # ISO date format check
        assert len(result["swing_high"]["date"]) == 10
        assert len(result["swing_low"]["date"]) == 10

    def test_price_at_exact_level_no_crash(self):
        """If current price matches a level exactly, no crash."""
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        result = mcp_tools._calculate_fibonacci(df, swing_high)

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
        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_sector_peers", mock_fetch)

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
        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_sector_peers", mock_fetch)

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

        monkeypatch.setattr(
            mcp_tools, "_get_finnhub_client", lambda: MockFinnhubClient()
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

        monkeypatch.setattr(mcp_tools.yf, "Ticker", MockTicker)

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

        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_sector_peers", mock_fetch)

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
        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_sector_peers", mock_fetch)

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
            mcp_tools.naver_finance,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_or_refresh_maps",
        AsyncMock(return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인"}}),
    )
    monkeypatch.setattr(
        mcp_tools,
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
    monkeypatch.setattr(
        mcp_tools, "_fetch_quote_equity_kr", AsyncMock(return_value={"price": 71000.0})
    )
    monkeypatch.setattr(
        mcp_tools, "_fetch_quote_equity_us", AsyncMock(return_value={"price": 220.0})
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_multiple_current_prices",
        AsyncMock(return_value={"KRW-BTC": 60000000.0}),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "ETH": "이더리움"}}
        ),
    )
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC", "KRW-ETH"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 61000000.0, "KRW-ETH": 4200000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "DOGE": "도지"}}
        ),
    )
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC", "KRW-DOGE"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert sorted(markets) == ["KRW-BTC", "KRW-DOGE"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
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
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
        mcp_tools.upbit_service,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_or_refresh_maps",
        AsyncMock(
            return_value={"COIN_TO_NAME_KR": {"BTC": "비트코인", "PCI": "페이코인"}}
        ),
    )
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    monkeypatch.setattr(
        mcp_tools.upbit_service,
        "fetch_all_market_codes",
        AsyncMock(return_value=["KRW-BTC"]),
    )

    async def mock_fetch(markets: list[str]) -> dict[str, float]:
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 62000000.0}

    quote_mock = AsyncMock(side_effect=mock_fetch)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools.upbit_service,
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
    monkeypatch.setattr(
        mcp_tools,
        "get_or_refresh_maps",
        AsyncMock(return_value={"COIN_TO_NAME_KR": {"ETH": "이더리움"}}),
    )
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )
    quote_mock = AsyncMock(return_value={"KRW-ETH": 4300000.0})
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_multiple_current_prices", quote_mock
    )

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

    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools,
        "_collect_manual_positions",
        AsyncMock(return_value=([], [])),
    )

    async def mock_fetch_yahoo_raise(symbol: str) -> dict[str, object]:
        raise ValueError(f"Symbol '{symbol}' not found")

    monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_us", mock_fetch_yahoo_raise)

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

    monkeypatch.setattr(
        mcp_tools,
        "_collect_portfolio_positions",
        AsyncMock(return_value=(mocked_positions, [], "equity_kr", None)),
    )

    result = await tools["get_position"]("005930", market="kr")
    assert result["has_position"] is True
    assert result["status"] == "보유"
    assert result["position_count"] == 2
    assert sorted(result["accounts"]) == ["kis", "toss"]

    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(
        mcp_tools,
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
        mcp_tools._COINGECKO_LIST_CACHE["expires_at"] = 0.0
        mcp_tools._COINGECKO_LIST_CACHE["symbol_to_ids"] = {}
        mcp_tools._COINGECKO_PROFILE_CACHE.clear()

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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

        monkeypatch.setattr("app.mcp_server.tools.httpx.AsyncClient", MockClient)

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

    monkeypatch.setattr(
        mcp_tools,
        "_fetch_ohlcv_for_indicators",
        AsyncMock(return_value=base_df[["date", "high", "low", "close"]]),
    )
    monkeypatch.setattr(
        mcp_tools,
        "_fetch_ohlcv_for_volume_profile",
        AsyncMock(return_value=base_df),
    )
    monkeypatch.setattr(
        mcp_tools,
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
    monkeypatch.setattr(
        mcp_tools,
        "_calculate_volume_profile",
        lambda df, bins, value_area_ratio=0.70: {
            "price_range": {"low": 80.0, "high": 120.0},
            "poc": {"price": 90.0, "volume": 5000.0},
            "value_area": {"high": 111.0, "low": 89.0, "volume_pct": 70.0},
            "profile": [],
        },
    )
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())

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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())
    monkeypatch.setattr(
        mcp_tools,
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

    monkeypatch.setattr(mcp_tools, "upbit_service", DummyUpbit())

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

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools,
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
                    "crcy_cd": "USD",
                    "frcr_dncl_amt_2": "100.0",
                    "frcr_ord_psbl_amt": "100.0",
                }
            ]

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        mcp_tools,
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
async def test_place_order_balance_lookup_failure_returns_query_error(monkeypatch):
    """Balance lookup failure should return API error, not 0-balance warning."""
    tools = build_tools()

    class FailingKISClient:
        async def inquire_domestic_cash_balance(self):
            raise RuntimeError("KIS balance lookup failed")

    async def fetch_quote(symbol):
        return {"price": 5000.0}

    monkeypatch.setattr(mcp_tools, "KISClient", FailingKISClient)
    monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_kr", fetch_quote)

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

    monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)
    monkeypatch.setattr(
        mcp_tools, "get_exchange_by_symbol", lambda s: "NYSE" if s == "TSM" else None
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
        result = mcp_tools._compute_rsi_weights(25.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first step gets most weight
        assert result[0] > result[1] > result[2]

    def test_oversold_with_four_splits(self):
        """RSI < 30 with splits=4."""
        result = mcp_tools._compute_rsi_weights(28.0, 4)

        assert len(result) == 4
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first > last, monotonically decreasing
        assert result[0] > result[-1]
        assert result[0] > result[1] > result[2] > result[3]

    def test_overbought_returns_back_heavy_weights(self):
        """RSI > 50: linear increasing weights (more later)."""
        result = mcp_tools._compute_rsi_weights(65.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Back-heavy: last step gets most weight
        assert result[2] > result[1] > result[0]

    def test_neutral_returns_equal_weights(self):
        """RSI 30-50: equal distribution."""
        result = mcp_tools._compute_rsi_weights(40.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # All weights equal
        assert all(abs(w - result[0]) < 0.001 for w in result)

    def test_none_rsi_returns_equal_weights(self):
        """None RSI: equal distribution (same as neutral)."""
        result = mcp_tools._compute_rsi_weights(None, 3)

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

        result = mcp_tools._compute_dca_price_levels(
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

        result = mcp_tools._compute_dca_price_levels(
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

        result = mcp_tools._compute_dca_price_levels(
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

        result = mcp_tools._compute_dca_price_levels(
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

        result = mcp_tools._compute_dca_price_levels(
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

        result = mcp_tools._compute_dca_price_levels(
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
            mcp_tools._compute_dca_price_levels("invalid", 3, current_price, supports)


@pytest.mark.asyncio
class TestCreateDcaPlan:
    """Tests for create_dca_plan MCP tool."""

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

        monkeypatch.setattr(mcp_tools, "_get_support_resistance_impl", mock_sr)
        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_indicators)

        # Mock DB session and DcaService.create_plan so persistence succeeds for dry_run
        db = AsyncMock()
        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr(mcp_tools, "_get_support_resistance_impl", mock_sr)
        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_indicators)

        # Mock DB session and DcaService.create_plan so persistence succeeds for dry_run
        db = AsyncMock()
        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr(mcp_tools, "_get_support_resistance_impl", mock_sr)
        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_indicators)

        # Mock DB session factory
        db = AsyncMock()
        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr(mcp_tools, "_place_order_impl", mock_place_order_impl)

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
        mcp_tools.register_tools(mcp)
        return mcp.tools

    @pytest.mark.asyncio
    async def test_get_dca_status_by_plan_id(self, monkeypatch):
        """Test get_dca_status by plan_id (highest priority)."""
        tools = build_tools()

        # Mock database
        db = AsyncMock()
        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(
            mcp_tools,
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
        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr(mcp_tools, "_place_order_impl", mock_place_order)

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

        monkeypatch.setattr(mcp_tools, "_get_support_resistance_impl", mock_sr)
        monkeypatch.setattr(mcp_tools, "_get_indicators_impl", mock_indicators)

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
        monkeypatch.setattr(
            mcp_tools,
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

        monkeypatch.setattr(mcp_tools, "_get_dca_status_impl", mock_impl)

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
        mcp_tools.register_tools(mcp)
        return mcp.tools

    @pytest.mark.asyncio
    async def test_place_order_high_amount_kr_equity(self, monkeypatch):
        """place_order accepts high-amount orders (> 1M KRW) for KR equity."""
        tools = build_tools()

        async def fetch_quote(symbol):
            return {"price": 100000.0}

        monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_kr", fetch_quote)

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

        async def fetch_quote(symbol):
            return {"price": 205.0}

        monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_us", fetch_quote)

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
            mcp_tools.upbit_service,
            "fetch_multiple_current_prices",
            mock.fetch_multiple_current_prices,
        )
        monkeypatch.setattr(
            mcp_tools.upbit_service,
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
                    "stck_cash_ord_psbl_amt": "100000000.0",
                    "usd_ord_psbl_amt": "0",
                    "dnca_tot_amt": "0",
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

        monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)
        monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_kr", fetch_quote)

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

        monkeypatch.setattr(mcp_tools, "KISClient", MockKISClient)
        monkeypatch.setattr(mcp_tools, "_fetch_quote_equity_us", fetch_quote)
        monkeypatch.setattr(mcp_tools, "get_exchange_by_symbol", lambda _: "NASD")

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
            mcp_tools.upbit_service,
            "fetch_multiple_current_prices",
            mock.fetch_multiple_current_prices,
        )
        monkeypatch.setattr(
            mcp_tools.upbit_service,
            "fetch_my_coins",
            mock.fetch_my_coins,
        )
        monkeypatch.setattr(
            mcp_tools.upbit_service,
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
            mcp_tools.upbit_service,
            "fetch_multiple_current_prices",
            mock.fetch_multiple_current_prices,
        )
        monkeypatch.setattr(
            mcp_tools.upbit_service,
            "fetch_my_coins",
            mock.fetch_my_coins,
        )
        monkeypatch.setattr(
            mcp_tools.upbit_service,
            "place_market_buy_order",
            mock.place_market_buy_order,
        )
        monkeypatch.setattr(
            mcp_tools.settings, "redis_url", "redis://localhost:6379/0", raising=False
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
