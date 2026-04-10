"""
Tests for MCP quotes/search/dividends tools.

This module contains tests for:
- search_symbol: Symbol search across markets (KR, US, crypto)
- get_quote: Real-time price quotes across markets
- get_dividends: Dividend information for US equities

These tests were extracted from tests/test_mcp_server_tools.py for better organization.
"""

from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    _single_row_df,
    build_tools,
)

# ---------------------------------------------------------------------------
# search_symbol Tests
# ---------------------------------------------------------------------------


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
        "search_kr_symbols",
        AsyncMock(
            return_value=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "instrument_type": "equity_kr",
                    "exchange": "KOSPI",
                    "is_active": True,
                },
                {
                    "symbol": "006400",
                    "name": "삼성SDI",
                    "instrument_type": "equity_kr",
                    "exchange": "KOSPI",
                    "is_active": True,
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "search_us_symbols",
        AsyncMock(return_value=[]),
    )
    _patch_runtime_attr(
        monkeypatch,
        "search_upbit_symbols",
        AsyncMock(return_value=[]),
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
        "search_us_symbols",
        AsyncMock(
            return_value=[
                {
                    "symbol": "AAPL",
                    "name": "애플",
                    "instrument_type": "equity_us",
                    "exchange": "NASDAQ",
                    "is_active": True,
                }
            ]
        ),
    )

    # Search with us market filter
    result = await tools["search_symbol"]("애플", market="us")

    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_search_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()

    async def raise_error(*_args, **_kwargs):
        raise RuntimeError("master data failed")

    _patch_runtime_attr(monkeypatch, "search_kr_symbols", raise_error)

    result = await tools["search_symbol"]("samsung")

    assert len(result) == 1
    assert result[0]["error"] == "master data failed"
    assert result[0]["source"] == "master"
    assert result[0]["query"] == "samsung"


# ---------------------------------------------------------------------------
# get_quote Tests - Crypto
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# get_quote Tests - Korean Equity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0  # price = close
    assert result["open"] == 100.0
    assert called["code"] == "005930"
    assert called["market"] == "J"
    assert called["n"] == 1


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


# ---------------------------------------------------------------------------
# get_quote Tests - US Equity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    tools = build_tools()

    mock_fetch_fast_info = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "close": 205.0,
            "previous_close": 201.5,
            "open": 202.0,
            "high": 206.2,
            "low": 200.8,
            "volume": 123456789,
        }
    )
    monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fetch_fast_info)

    result = await tools["get_quote"]("AAPL")

    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["price"] == 205.0
    assert result["previous_close"] == 201.5
    assert result["open"] == 202.0
    assert result["high"] == 206.2
    assert result["low"] == 200.8
    assert result["volume"] == 123456789
    mock_fetch_fast_info.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_equity_propagates_upstream_exception(monkeypatch):
    tools = build_tools()

    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=RuntimeError("yahoo down")),
    )

    with pytest.raises(RuntimeError, match="yahoo down"):
        await tools["get_quote"]("AAPL")


# ---------------------------------------------------------------------------
# get_quote Tests - Error Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_non_us_markets_keep_error_payload_contract(monkeypatch):
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
async def test_get_quote_raises_on_invalid_symbol():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_quote"]("")

    # Note: Numeric symbols like "1234" are now normalized to "001234" for KR market,
    # so we test with a clearly invalid format instead
    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("!@#$")


# ---------------------------------------------------------------------------
# get_quote Tests - Market Parameter Validation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# get_dividends Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dividends_uses_session_and_keeps_payload(monkeypatch):
    tools = build_tools()
    captured: dict[str, object] = {}

    class MockTicker:
        info = {
            "dividendYield": 0.01234,
            "dividendRate": 1.11,
            "exDividendDate": 1704067200,
        }
        dividends = pd.Series(
            [1.0, 1.2],
            index=pd.to_datetime(["2024-01-01", "2024-04-01"]),
        )

    def ticker_factory(symbol, session=None):
        captured["symbol"] = symbol
        captured["session"] = session
        return MockTicker()

    monkeypatch.setattr("yfinance.Ticker", ticker_factory)

    result = await tools["get_dividends"]("aapl")

    assert result["success"] is True
    assert result["symbol"] == "AAPL"
    assert result["dividend_yield"] == 0.0123
    assert result["dividend_rate"] == 1.11
    assert result["ex_dividend_date"] == "2024-01-01"
    assert result["last_dividend"] == {"date": "2024-04-01", "amount": 1.2}
    assert captured["symbol"] == "AAPL"
    assert captured["session"] is not None
