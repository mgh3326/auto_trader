from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server import tools as mcp_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    async def __aenter__(self):
        return object()

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
async def test_search_symbol_empty_query_returns_empty():
    tools = build_tools()

    result = await tools["search_symbol"]("   ")

    assert result == []


@pytest.mark.asyncio
async def test_search_symbol_clamps_limit_and_shapes(monkeypatch):
    tools = build_tools()
    called = {}

    class DummyStockInfoService:
        def __init__(self, db) -> None:
            self.db = db

        async def search_stocks(self, query: str, limit: int):
            called["query"] = query
            called["limit"] = limit
            return [
                SimpleNamespace(
                    symbol="005930",
                    name="Samsung Electronics",
                    instrument_type="equity_kr",
                    exchange="KOSPI",
                    is_active=True,
                )
            ]

    monkeypatch.setattr(mcp_tools, "AsyncSessionLocal", lambda: DummySessionManager())
    monkeypatch.setattr(mcp_tools, "StockInfoService", DummyStockInfoService)

    result = await tools["search_symbol"]("  samsung  ", limit=500)

    assert called["query"] == "samsung"
    assert called["limit"] == 100
    assert result == [
        {
            "symbol": "005930",
            "name": "Samsung Electronics",
            "instrument_type": "equity_kr",
            "exchange": "KOSPI",
            "is_active": True,
        }
    ]


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
    assert result["open"] == 100.0
    assert result["close"] == 105.0


@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_price", mock_fetch)

    result = await tools["get_quote"]("AAPL")

    mock_fetch.assert_awaited_once_with("AAPL")
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["open"] == 100.0
    assert result["close"] == 105.0


@pytest.mark.asyncio
async def test_get_quote_raises_on_invalid_symbol():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_quote"]("")

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("1234")


@pytest.mark.asyncio
async def test_get_ohlcv_crypto(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", days=300)

    mock_fetch.assert_awaited_once_with(market="KRW-BTC", days=200)
    assert result["instrument_type"] == "crypto"
    assert result["source"] == "upbit"
    assert result["days"] == 200
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", days=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["days"] == 10
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", days=5)

    mock_fetch.assert_awaited_once_with(ticker="AAPL", days=5)
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["days"] == 5
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_input():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_ohlcv"]("")

    with pytest.raises(ValueError, match="days must be > 0"):
        await tools["get_ohlcv"]("AAPL", days=0)

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_ohlcv"]("1234")
