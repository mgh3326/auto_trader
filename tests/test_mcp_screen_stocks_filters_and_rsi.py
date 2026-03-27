import logging
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling import analysis_screen_core
from app.mcp_server.tooling.screening import crypto as screening_crypto
from app.mcp_server.tooling.screening import kr as screening_kr
from app.services import naver_finance
from tests._mcp_tooling_support import build_tools

pytest_plugins = ("tests._mcp_tooling_support",)


class TestScreenStocksRsiLogging:
    @pytest.mark.asyncio
    async def test_kr_rsi_uses_short_code_over_code(self, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        called_symbols: list[tuple[str, str, int]] = []

        async def mock_fetch_ohlcv(symbol, market_type, count):
            called_symbols.append((symbol, market_type, count))
            return pd.DataFrame({"close": [100.0 + i for i in range(50)]})

        def mock_calculate_rsi(close):
            return {"14": 42.0}

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        monkeypatch.setattr(screening_kr, "_calculate_rsi", mock_calculate_rsi)

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert called_symbols, "OHLCV fetch should be called for RSI enrichment"
        assert called_symbols[0][0] == "005930"
        assert called_symbols[0][1] == "equity_kr"
        assert result["results"][0]["rsi"] == 42.0

    @pytest.mark.asyncio
    async def test_kr_rsi_ohlcv_exception_logs_error(self, monkeypatch, caplog):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RuntimeError("boom-kr")

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        caplog.set_level(logging.ERROR)
        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert result["results"][0].get("rsi") is None
        assert any("[RSI-KR] ❌ Failed" in record.message for record in caplog.records)
        assert any("RuntimeError" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_kr_rsi_empty_or_malformed_ohlcv_keeps_base_rows(self, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    },
                    {
                        "code": "KR7000660001",
                        "short_code": "000660",
                        "name": "SK하이닉스",
                        "close": 150000.0,
                        "volume": 900,
                        "market_cap": 900_000,
                    },
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            assert market_type == "equity_kr"
            assert count == 50
            if symbol == "005930":
                return pd.DataFrame()
            return pd.DataFrame({"date": pd.to_datetime(["2026-03-07"]), "open": [1.0]})

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 2
        assert [item["code"] for item in result["results"]] == [
            "KR7005930003",
            "KR7000660001",
        ]
        assert all(item.get("rsi") is None for item in result["results"])
        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 2
        assert diagnostics["succeeded"] == 0
        assert diagnostics["failed"] == 2
        assert diagnostics["error_samples"]
        assert diagnostics["error_samples"][0] == "Missing OHLCV close data"

    @pytest.mark.asyncio
    async def test_kr_rsi_rate_limited_diagnostic_counts(self, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RateLimitExceededError("KIS rate limit retries exhausted")

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["succeeded"] == 0
        assert diagnostics["rate_limited"] == 1
        assert diagnostics["failed"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_enrichment_handles_generic_exception(self, monkeypatch, caplog):
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ],
            "returned_count": 1,
            "warnings": ["parallel execution returned unexpected shape"],
            "meta": {},
            "filters_applied": {},
            "market": "crypto",
        }

        monkeypatch.setattr(
            screening_crypto,
            "_screen_crypto_via_tvscreener",
            AsyncMock(return_value=mock_result),
        )

        caplog.set_level(logging.WARNING)
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert any(
            "parallel execution returned unexpected shape" in warning
            for warning in result.get("warnings", [])
        )


class TestScreenStocksFilters:
    @pytest.mark.asyncio
    async def test_kr_min_market_cap(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=100000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 100000000000

    @pytest.mark.asyncio
    async def test_us_min_market_cap(self, mock_yfinance_screen, monkeypatch):
        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 1000000000
        assert "error" not in result, f"Unexpected error: {result.get('error')}"

    @pytest.mark.asyncio
    async def test_crypto_market_cap_filter_warning(self, mock_upbit_coins, monkeypatch):
        mock_result = {
            "results": [],
            "total_count": 0,
            "returned_count": 0,
            "filters_applied": {"min_market_cap": 300000000000},
            "market": "crypto",
            "warnings": ["min_market_cap filter is not supported for crypto market; ignored"],
            "meta": {},
        }

        monkeypatch.setattr(
            screening_crypto,
            "_screen_crypto_via_tvscreener",
            AsyncMock(return_value=mock_result),
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=300000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 300000000000
        assert "warnings" in result
        assert any(
            "min_market_cap" in w and "not supported" in w for w in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_kr_min_market_cap_only_no_naver_queries(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        naver_finance_called = False

        async def mock_fetch_valuation(code):
            nonlocal naver_finance_called
            naver_finance_called = True
            return {}

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=100000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 100000
        assert not naver_finance_called, (
            "Naver Finance should not be called for min_market_cap only"
        )


class TestScreenStocksSorting:
    @pytest.mark.asyncio
    async def test_kr_sort_by_volume_desc(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["sort_by"] == "volume"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    async def test_us_sort_by_change_rate_asc(self, monkeypatch):
        import yfinance as yf

        def mock_yfinance_screen_func(query, size, sortField, sortAsc, session=None):
            assert session is not None
            return {
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "shortname": "Apple Inc.",
                        "lastprice": 175.5,
                        "percentchange": -1.0,
                        "dayvolume": 50000000,
                        "intradaymarketcap": 2800000000000,
                        "peratio": 28.5,
                        "forward_dividend_yield": 0.005,
                    },
                    {
                        "symbol": "MSFT",
                        "shortname": "Microsoft Corp",
                        "lastprice": 330.0,
                        "percentchange": 0.5,
                        "dayvolume": 20000000,
                        "intradaymarketcap": 2500000000000,
                        "peratio": 32.0,
                        "forward_dividend_yield": 0.008,
                    },
                ]
            }

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen_func)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="asc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["sort_by"] == "change_rate"
        assert result["filters_applied"]["sort_order"] == "asc"
        assert "error" not in result, f"Unexpected error: {result.get('error')}"


class TestScreenStocksLimit:
    @pytest.mark.asyncio
    async def test_limit_enforcement(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks * 5

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result is not None
        assert len(result["results"]) <= 5
        assert result["returned_count"] <= 5


class TestScreenStocksDividendYieldNormalization:
    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_decimal_input(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=0.03,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 0.03
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.03

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_percent_input(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=3.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 3.0
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.03

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_one_percent_input(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=1.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 1.0
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.01

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_equivalence(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result_decimal = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=0.03,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )
        result_percent = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=3.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert (
            result_decimal["filters_applied"]["min_dividend_yield_normalized"]
            == result_percent["filters_applied"]["min_dividend_yield_normalized"]
        )
        assert result_decimal["filters_applied"]["min_dividend_yield_input"] == 0.03
        assert result_percent["filters_applied"]["min_dividend_yield_input"] == 3.0

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_none_input(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert "min_dividend_yield_input" not in result["filters_applied"]
        assert "min_dividend_yield_normalized" not in result["filters_applied"]


class TestScreenStocksPhase2Spec:
    @pytest.mark.asyncio
    async def test_kr_etf_category_semiconductor(self, mock_krx_etfs, monkeypatch):
        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category="반도체",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["asset_type"] == "etf"
        assert result["filters_applied"]["category"] == "반도체"
        if len(result["results"]) > 0:
            semiconductor_found = False
            for item in result["results"]:
                assert item.get("asset_type") == "etf", "All results should be ETFs"
                if "category" in item and "반도체" in item["category"]:
                    semiconductor_found = True
                    break
            assert semiconductor_found, "Should find at least one semiconductor ETF"

    @pytest.mark.asyncio
    async def test_kr_etf_has_asset_type_and_category(self, mock_krx_etfs, monkeypatch):
        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="etf",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert len(result["results"]) > 0, "Should have ETF results"
        for item in result["results"]:
            assert item.get("asset_type") == "etf", (
                "All ETFs should have asset_type='etf'"
            )
            assert "category" in item, "All ETFs should have category field"
            assert isinstance(item["category"], str), "Category should be a string"

    @pytest.mark.asyncio
    async def test_kr_market_cap_unit_100m_won(self, mock_krx_stocks, monkeypatch):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["min_market_cap"] == 200000
            assert kwargs["sort_by"] == "market_cap"
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 80000.0,
                        "change_percent": 2.5,
                        "volume": 1000.0,
                        "market_cap": 4_800_000,
                        "rsi": 35.0,
                        "adx": 20.0,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "min_market_cap": 200000,
                    "sort_by": "market_cap",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=200000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["min_market_cap"] == 200000
        assert result["total_count"] == 1, "Only 삼성전자 should pass filter"
        assert len(result["results"]) == 1
        assert result["results"][0]["code"] == "005930"
        assert result["results"][0]["name"] == "Samsung Electronics Co., Ltd."
        assert result["results"][0]["market_cap"] == 4800000

    @pytest.mark.asyncio
    async def test_us_early_return_filters_applied_complete(self, monkeypatch):
        def mock_screen_none(query, size, sortField, sortAsc, session=None):
            assert session is not None
            return None

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_screen_none)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=25.0,
            min_dividend_yield=0.02,
            max_rsi=70,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert "min_market_cap" in result["filters_applied"]
        assert "max_per" in result["filters_applied"]
        assert "min_dividend_yield_normalized" in result["filters_applied"]
        assert "max_rsi" in result["filters_applied"]
        assert "sort_by" in result["filters_applied"]
        assert "sort_order" in result["filters_applied"]

    @pytest.mark.asyncio
    async def test_us_max_rsi_filter_applied(self, mock_yfinance_screen, monkeypatch):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["max_rsi"] == 70
            assert kwargs["sort_by"] == "volume"
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 180.0,
                        "change_percent": 1.0,
                        "volume": 1000.0,
                        "rsi": 65.0,
                    },
                    {
                        "symbol": "GOOGL",
                        "name": "Alphabet Inc.",
                        "price": 140.0,
                        "change_percent": 0.5,
                        "volume": 900.0,
                        "rsi": 60.0,
                    },
                ],
                "count": 2,
                "filters_applied": {"max_rsi": 70, "sort_by": "volume"},
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=70,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["filters_applied"]["max_rsi"] == 70
        assert result["total_count"] >= result["returned_count"]
        assert result["total_count"] == 2
        assert result["returned_count"] == 2
        assert [item["code"] for item in result["results"]] == ["AAPL", "GOOGL"]
        assert result["returned_count"] <= 2

    @pytest.mark.asyncio
    async def test_limit_zero_error(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="limit|at least 1"):
            await tools["screen_stocks"](
                market="kr",
                asset_type="stock",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=0,
            )

    @pytest.mark.asyncio
    async def test_limit_over_100_capped(self, mock_krx_stocks, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=100,
        )

        assert result is not None
        assert result["returned_count"] <= 100

    @pytest.mark.asyncio
    async def test_strategy_preset_with_case_insensitive_inputs(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="KOSPI",
            asset_type="STOCK",
            category=None,
            strategy="MOMENTUM",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="asc",
            limit=20,
        )

        assert result["market"] == "kospi"
        assert result["filters_applied"]["sort_by"] == "change_rate"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    async def test_crypto_rsi_enrichment_without_filters(self, mock_upbit_coins, monkeypatch):
        mock_result = {
            "results": [{"symbol": "KRW-BTC"}],
            "market": "crypto",
            "filters_applied": {"sort_by": "trade_amount", "sort_order": "desc"},
            "meta": {},
        }

        monkeypatch.setattr(
            screening_crypto,
            "_screen_crypto_via_tvscreener",
            AsyncMock(return_value=mock_result),
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            strategy="high_volume",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_order="asc",
            limit=20,
        )

        assert result["market"] == "crypto"
        assert result["filters_applied"]["sort_by"] == "trade_amount"
        assert result["filters_applied"]["sort_order"] == "desc"
