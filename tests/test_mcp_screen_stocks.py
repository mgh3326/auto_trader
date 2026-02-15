"""Tests for screen_stocks MCP tool."""

import logging

import pytest

from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling import analysis_screen_core
from app.mcp_server.tooling.registry import register_all_tools
from app.services import naver_finance
from app.services import upbit as upbit_service


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    register_all_tools(mcp)
    return mcp.tools


@pytest.fixture
def mock_krx_stocks():
    """Mock KRX stock data (market_cap in 억원)."""
    return [
        {
            "code": "005930",
            "name": "삼성전자",
            "close": 80000.0,
            "change_rate": 2.5,
            "change_price": 2000,
            "market": "KOSPI",
            "market_cap": 4800000,  # 480조원 = 4,800,000억원
        },
        {
            "code": "000660",
            "name": "SK하이닉스",
            "close": 150000.0,
            "change_rate": -1.2,
            "change_price": -1800,
            "market": "KOSPI",
            "market_cap": 150000,  # 15조원 = 150,000억원
        },
    ]


@pytest.fixture
def mock_krx_etfs():
    """Mock KRX ETF data (market_cap in 억원)."""
    return [
        {
            "code": "069500",
            "name": "KODEX 200",
            "close": 45000.0,
            "market": "KOSPI",
            "market_cap": 45000,  # 4.5조원 = 45,000억원
            "index_name": "KOSPI 200",
        },
        {
            "code": "114800",
            "name": "KODEX 반도체",
            "close": 12000.0,
            "market": "KOSPI",
            "market_cap": 1200,  # 1.2조원 = 1,200억원
            "index_name": "Wise 반도체지수",
        },
    ]


@pytest.fixture
def mock_valuation_data():
    """Mock valuation data from KRX."""
    return {
        "005930": {"per": 12.5, "pbr": 1.2, "dividend_yield": 0.0256},
        "000660": {"per": None, "pbr": None, "dividend_yield": None},
        "035420": {"per": 0, "pbr": 0.8, "dividend_yield": 0.035},
    }


class TestScreenStocksKRRegression:
    """Regression tests for KR market edge paths."""

    @pytest.mark.asyncio
    async def test_kr_change_rate_sort_desc(self, mock_krx_stocks, monkeypatch):
        """KR change_rate sorting should preserve positive/negative ordering."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="desc",
            limit=20,
        )

        assert result["returned_count"] == 2
        assert result["results"][0]["change_rate"] == 2.5
        assert result["results"][-1]["change_rate"] == -1.2

    @pytest.mark.asyncio
    async def test_submarket_routing_kospi_and_kosdaq(self, monkeypatch):
        """KOSPI/KOSDAQ should call only STK/KSQ source respectively."""

        calls: list[str] = []

        async def mock_fetch_stock_all_cached(market):
            calls.append(market)
            return []

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        await tools["screen_stocks"](
            market="kospi",
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
        assert calls == ["STK"]

        calls.clear()
        await tools["screen_stocks"](
            market="kosdaq",
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
        assert calls == ["KSQ"]

    @pytest.mark.asyncio
    async def test_kosdaq_skips_etf_fetch_when_asset_type_none(self, monkeypatch):
        """kosdaq should not fetch ETFs when asset_type is None."""

        etf_called = False

        async def mock_fetch_stock_all_cached(market):
            return []

        async def mock_fetch_etf_all_cached():
            nonlocal etf_called
            etf_called = True
            return []

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kosdaq",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["market"] == "kosdaq"
        assert not etf_called

    @pytest.mark.asyncio
    async def test_kr_batch_valuation_merge(
        self, mock_krx_stocks, mock_valuation_data, monkeypatch
    ):
        """Batch valuation data should be merged into KR results."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        async def mock_fetch_valuation_all_cached(market):
            return mock_valuation_data

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
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

        merged = {item["code"]: item for item in result["results"]}
        assert merged["005930"]["per"] == 12.5
        assert merged["005930"]["pbr"] == 1.2
        assert merged["005930"]["dividend_yield"] == 0.0256
        assert merged["000660"]["per"] is None
        assert merged["000660"]["pbr"] is None
        assert merged["000660"]["dividend_yield"] is None

    @pytest.mark.asyncio
    async def test_kr_valuation_fetch_failure_is_graceful(
        self, mock_krx_stocks, monkeypatch
    ):
        """Valuation fetch failure should not break KR screening."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        async def mock_fetch_valuation_all_cached(market):
            raise RuntimeError("KRX valuation temporary failure")

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
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

        assert "error" not in result
        assert result["returned_count"] == 2


@pytest.fixture
def mock_yfinance_screen():
    """Mock yfinance.screen function."""

    def mock_screen_func(query, size, sortField, sortAsc):
        return {
            "quotes": [
                {
                    "symbol": "AAPL",
                    "shortname": "Apple Inc.",
                    "lastprice": 175.5,
                    "percentchange": 1.2,
                    "dayvolume": 50000000,
                    "intradaymarketcap": 2800000000000,
                    "peratio": 28.5,
                    "forward_dividend_yield": 0.005,
                },
                {
                    "symbol": "MSFT",
                    "shortname": "Microsoft Corp",
                    "lastprice": 330.0,
                    "percentchange": -0.5,
                    "dayvolume": 20000000,
                    "intradaymarketcap": 2500000000000,
                    "peratio": 32.0,
                    "forward_dividend_yield": 0.008,
                },
                {
                    "symbol": "GOOGL",
                    "shortname": "Alphabet Inc.",
                    "lastprice": 140.0,
                    "percentchange": 0.8,
                    "dayvolume": 15000000,
                    "intradaymarketcap": 1500000000000,
                    "peratio": 22.0,
                    "forward_dividend_yield": 0.0,
                },
            ]
        }

    return mock_screen_func


class TestScreenStocksKR:
    """Test KR market functionality."""

    @pytest.mark.asyncio
    async def test_kr_stocks_default(self, mock_krx_stocks, monkeypatch):
        """Test KR stock screening with default parameters."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        assert "results" in result
        assert "total_count" in result
        assert "returned_count" in result
        assert "filters_applied" in result
        assert "timestamp" in result
        assert result["market"] == "kr"

    @pytest.mark.asyncio
    async def test_kr_etfs_default(self, mock_krx_etfs, monkeypatch):
        """Test KR ETF screening with default parameters."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            analysis_screen_core, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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

        assert result is not None
        assert result["market"] == "kr"
        assert len(result["results"]) > 0

    @pytest.mark.asyncio
    async def test_kr_auto_etf_on_category(self, mock_krx_etfs, monkeypatch):
        """Test KR auto-limits to ETFs when category is specified."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            analysis_screen_core, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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

    @pytest.mark.asyncio
    async def test_kr_etn_not_supported(self):
        """Test KR ETN (Exchange Traded Note) raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not supported|ETN"):
            await tools["screen_stocks"](
                market="kr",
                asset_type="etn",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=20,
            )


class TestScreenStocksUS:
    """Test US market functionality."""

    @pytest.mark.asyncio
    async def test_us_stocks_default(self, mock_yfinance_screen, monkeypatch):
        """Test US stock screening with default parameters."""

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
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
        assert result["market"] == "us"
        assert len(result["results"]) >= 0
        assert "error" not in result, f"Unexpected error: {result.get('error')}"


@pytest.fixture
def mock_upbit_coins():
    """Mock Upbit top traded coins data."""
    return [
        {
            "market": "KRW-BTC",
            "korean_name": "비트코인",
            "trade_price": 100_000_000,
            "signed_change_rate": 0.01,
            "acc_trade_price_24h": 1_000_000_000_000,
        },
        {
            "market": "KRW-ETH",
            "korean_name": "이더리움",
            "trade_price": 5_000_000,
            "signed_change_rate": 0.02,
            "acc_trade_price_24h": 800_000_000_000,
        },
    ]


class TestScreenStocksCrypto:
    """Test Crypto market functionality."""

    @pytest.mark.asyncio
    async def test_crypto_default(self, mock_upbit_coins, monkeypatch):
        """Test crypto screening with default parameters."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
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
        assert result["market"] == "crypto"
        assert len(result["results"]) > 0
        # Verify sort_by and sort_order are always recorded
        assert result["filters_applied"]["sort_by"] == "volume"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    async def test_crypto_per_filter_raises_error(self, mock_upbit_coins, monkeypatch):
        """Test crypto market raises ValueError for PER filter."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        with pytest.raises(ValueError, match=".*does not support.*max_per.*"):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=20.0,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_dividend_filter_raises_error(
        self, mock_upbit_coins, monkeypatch
    ):
        """Test crypto market raises ValueError for dividend filter."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        with pytest.raises(
            ValueError, match=".*does not support.*min_dividend_yield.*"
        ):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=0.03,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_kr_sort_by_rsi_raises_error(self, mock_krx_stocks, monkeypatch):
        """Test KR market raises ValueError for sort_by='rsi'."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        with pytest.raises(
            ValueError, match="RSI sorting is only supported for crypto"
        ):
            await tools["screen_stocks"](
                market="kr",
                asset_type="stock",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="rsi",
                sort_order="asc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_us_sort_by_rsi_raises_error(self, monkeypatch):
        """Test US market raises ValueError for sort_by='rsi'."""

        import yfinance as yf

        def mock_yfinance_screen_func(query, size, sortField, sortAsc):
            return {"quotes": []}

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen_func)

        tools = build_tools()

        with pytest.raises(
            ValueError, match="RSI sorting is only supported for crypto"
        ):
            await tools["screen_stocks"](
                market="us",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="rsi",
                sort_order="asc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_enriches_metrics_without_explicit_rsi_filters(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        rsi_fetch_called = False

        async def mock_fetch_ohlcv(symbol, market_type, count):
            nonlocal rsi_fetch_called
            rsi_fetch_called = True
            raise RuntimeError("expected fetch for enrichment")

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert rsi_fetch_called
        assert result["meta"]["rsi_enrichment"]["attempted"] > 0
        assert all("score" in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_sort_by_rsi_fetches_rsi(self, mock_upbit_coins, monkeypatch):
        """Test crypto market fetches RSI when sort_by='rsi'."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        import pandas as pd

        async def mock_fetch_ohlcv(symbol, market_type, count):
            return pd.DataFrame({"close": [100.0 + i for i in range(50)]})

        def mock_calculate_rsi(close):
            return {"14": 45.0}

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        monkeypatch.setattr(analysis_screen_core, "_calculate_rsi", mock_calculate_rsi)

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
            sort_order="asc",
            limit=20,
        )

        assert result["meta"]["rsi_enrichment"]["attempted"] > 0


class TestScreenStocksRsiLogging:
    """Test RSI enrichment logging and symbol selection behavior."""

    @pytest.mark.asyncio
    async def test_kr_rsi_uses_short_code_over_code(self, monkeypatch):
        """KR RSI enrichment should prefer short_code for KIS OHLCV lookup."""

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
            import pandas as pd

            called_symbols.append((symbol, market_type, count))
            return pd.DataFrame({"close": [100.0 + i for i in range(50)]})

        def mock_calculate_rsi(close):
            return {"14": 42.0}

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        monkeypatch.setattr(analysis_screen_core, "_calculate_rsi", mock_calculate_rsi)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
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

        assert called_symbols, "OHLCV fetch should be called for RSI enrichment"
        assert called_symbols[0][0] == "005930"
        assert called_symbols[0][1] == "equity_kr"
        assert result["results"][0]["rsi"] == 42.0

    @pytest.mark.asyncio
    async def test_crypto_rsi_falls_back_to_market_field(self, monkeypatch, caplog):
        """Crypto RSI enrichment should fall back to item['market'] when code is missing."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        called_symbols: list[str] = []

        async def mock_fetch_ohlcv(symbol, market_type, count):
            called_symbols.append(symbol)
            raise RuntimeError("should not be called")

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        caplog.set_level(logging.ERROR)
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
            sort_order="asc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert called_symbols == ["crypto"]
        assert any(
            "[RSI-Crypto] ❌ Failed for crypto" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_kr_rsi_ohlcv_exception_logs_error(self, monkeypatch, caplog):
        """KR RSI enrichment should log errors and return item without RSI on OHLCV failure."""

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
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        caplog.set_level(logging.ERROR)
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
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

        assert result["returned_count"] == 1
        assert result["results"][0].get("rsi") is None
        assert any("[RSI-KR] ❌ Failed" in record.message for record in caplog.records)
        assert any("RuntimeError" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_crypto_rsi_ohlcv_exception_logs_error(self, monkeypatch, caplog):
        """Crypto RSI enrichment should log errors and continue on OHLCV failure."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RuntimeError("boom-crypto")

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        caplog.set_level(logging.ERROR)
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
        assert result["results"][0].get("rsi") is None
        assert any(
            "[RSI-Crypto] ❌ Failed" in record.message for record in caplog.records
        )
        assert any("RuntimeError" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_kr_rsi_rate_limited_diagnostic_counts(self, monkeypatch):
        """KR RSI enrichment should surface rate-limited diagnostics."""

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
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
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

        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["succeeded"] == 0
        assert diagnostics["rate_limited"] == 1
        assert diagnostics["failed"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_rate_limited_diagnostic_counts(self, monkeypatch):
        """Crypto RSI enrichment should surface rate-limited diagnostics when enrich_rsi=True."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RateLimitExceededError("Upbit rate limit retries exhausted")

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
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

        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["succeeded"] == 0
        assert diagnostics["rate_limited"] == 1
        assert diagnostics["failed"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_gather_exception_is_logged(self, monkeypatch, caplog):
        """Crypto RSI gather-level exception should be logged and skipped."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        async def mock_gather(*aws, **kwargs):
            for awaitable in aws:
                awaitable.close()
            return [RuntimeError("forced gather failure")]

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(analysis_screen_core.asyncio, "gather", mock_gather)

        caplog.set_level(logging.ERROR)
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
            "gather returned exception" in record.message for record in caplog.records
        )


class TestScreenStocksFilters:
    """Test filter application."""

    @pytest.mark.asyncio
    async def test_kr_min_market_cap(self, mock_krx_stocks, monkeypatch):
        """Test KR market with minimum market cap filter."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test US market with minimum market cap filter."""

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
    async def test_crypto_min_market_cap(self, mock_upbit_coins, monkeypatch):
        """Test crypto market with minimum market cap filter - not supported, warning added."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
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
            sort_by="volume",
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
        """Test KR market with min_market_cap only - Naver Finance not called, but RSI enrichment still runs."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        # Track whether Naver Finance valuation queries are called (they shouldn't be)
        naver_finance_called = False

        async def mock_fetch_valuation(code):
            nonlocal naver_finance_called
            naver_finance_called = True
            return {}

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        # RSI enrichment IS expected to run even with min_market_cap only
        # (policy: RSI auto-enrichment is maintained)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=100000,  # Only basic filter
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 100000
        # Verify Naver Finance was NOT called (uses KRX batch valuation instead)
        assert not naver_finance_called, (
            "Naver Finance should not be called for min_market_cap only"
        )


class TestScreenStocksSorting:
    """Test sorting functionality."""

    @pytest.mark.asyncio
    async def test_kr_sort_by_volume_desc(self, mock_krx_stocks, monkeypatch):
        """Test KR market sorted by volume descending."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test US market sorted by change rate ascending."""

        import yfinance as yf

        def mock_yfinance_screen_func(query, size, sortField, sortAsc):
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
    """Test limit parameter."""

    @pytest.mark.asyncio
    async def test_limit_enforcement(self, mock_krx_stocks, monkeypatch):
        """Test that limit parameter is properly enforced."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks * 5

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
    """Test dividend yield input normalization (decimal vs percentage)."""

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_decimal_input(
        self, mock_krx_stocks, monkeypatch
    ):
        """Test KR market with decimal dividend yield input (0.03)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test KR market with percentage dividend yield input (3.0)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test KR market with 1.0 input interpreted as 1% (0.01)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test that decimal (0.03) and percent (3.0) inputs produce identical results."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test KR market with None dividend yield input - no input/normalized keys."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
    """Test Phase 2 specification compliance."""

    @pytest.mark.asyncio
    async def test_kr_etf_category_semiconductor(self, mock_krx_etfs, monkeypatch):
        """Test KR ETF category filtering with '반도체' category."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            analysis_screen_core, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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

        # Verify at least one result matches semiconductor category
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
        """Test KR ETF results have asset_type='etf' and category field."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            analysis_screen_core, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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
        """Test KR min_market_cap filter uses 억원 (100 million KRW) unit."""

        async def mock_fetch_stock_all_cached(market):
            # Return different stocks for different markets to avoid duplicates
            if market == "STK":  # KOSPI
                return [mock_krx_stocks[0]]  # 삼성전자 only
            elif market == "KSQ":  # KOSDAQ
                return [mock_krx_stocks[1]]  # SK하이닉스 only
            return []

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        # Filter by min_market_cap=200000 (200,000억원 = 20조원)
        # Should only return 삼성전자 (4,800,000억원)
        # SK하이닉스 (150,000억원) should be filtered out
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
        assert result["results"][0]["name"] == "삼성전자"
        assert result["results"][0]["market_cap"] == 4800000

    @pytest.mark.asyncio
    async def test_us_early_return_filters_applied_complete(self, monkeypatch):
        """Test US market early-return includes all filters_applied fields."""

        def mock_screen_none(query, size, sortField, sortAsc):
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

        # Verify all filter keys are present even on early return
        assert "min_market_cap" in result["filters_applied"]
        assert "max_per" in result["filters_applied"]
        assert "min_dividend_yield_normalized" in result["filters_applied"]
        assert "max_rsi" in result["filters_applied"]
        assert "sort_by" in result["filters_applied"]
        assert "sort_order" in result["filters_applied"]

    @pytest.mark.asyncio
    async def test_us_max_rsi_filter_applied(self, mock_yfinance_screen, monkeypatch):
        """Test US market max_rsi filter is actually applied and total_count is correct."""

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)

        # Mock RSI calculation to return different values for different symbols
        async def mock_fetch_ohlcv(symbol, market_type, count):
            import pandas as pd

            # AAPL: RSI will be ~65 (below 70, passes)
            # MSFT: RSI will be ~75 (above 70, filtered out)
            # GOOGL: RSI will be ~60 (below 70, passes)
            if symbol == "MSFT":
                # Rising prices -> high RSI
                return pd.DataFrame(
                    {
                        "close": [100 + i * 2 for i in range(50)],
                        "open": [100 + i * 2 for i in range(50)],
                        "high": [102 + i * 2 for i in range(50)],
                        "low": [99 + i * 2 for i in range(50)],
                        "volume": [1000000 for _ in range(50)],
                    }
                )
            else:
                # More stable prices -> moderate RSI
                return pd.DataFrame(
                    {
                        "close": [100 + (i % 5) for i in range(50)],
                        "open": [100 + (i % 5) for i in range(50)],
                        "high": [102 + (i % 5) for i in range(50)],
                        "low": [99 + (i % 5) for i in range(50)],
                        "volume": [1000000 for _ in range(50)],
                    }
                )

        monkeypatch.setattr(
            analysis_screen_core, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        tools = build_tools()

        # Request with max_rsi filter
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
        # total_count should be >= returned_count
        assert result["total_count"] >= result["returned_count"]
        # At least one stock should be filtered out by RSI
        # (yfinance mock returns 3, but MSFT should be filtered)
        assert result["total_count"] <= 3
        assert result["returned_count"] <= 2  # AAPL and GOOGL should pass

    @pytest.mark.asyncio
    async def test_limit_zero_error(self):
        """Test limit=0 raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="limit|between 1 and 50"):
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
    async def test_limit_over_50_capped(self, mock_krx_stocks, monkeypatch):
        """Test limit>50 is capped to 50 (not an error, by design)."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            analysis_screen_core, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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

        # Should not raise error, but cap to 50
        assert result is not None
        assert result["returned_count"] <= 50
