"""Tests for screen_stocks MCP tool."""

import asyncio
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


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    mcp_tools.register_tools(mcp)
    return mcp.tools


@pytest.fixture
def mock_krx_stocks():
    """Mock KRX stock data."""
    return [
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


@pytest.fixture
def mock_krx_etfs():
    """Mock KRX ETF data."""
    return [
        {
            "code": "069500",
            "name": "KODEX 200",
            "close": 45000.0,
            "market": "KOSPI",
            "market_cap": 4500000000000,
        },
        {
            "code": "114800",
            "name": "KODEX 반도체",
            "close": 12000.0,
            "market": "KOSPI",
            "market_cap": 120000000000,
        },
    ]


@pytest.fixture
def mock_upbit_coins():
    """Mock Upbit top traded coins."""
    return [
        {
            "market": "KRW-BTC",
            "korean_name": "비트코인",
            "english_name": "Bitcoin",
            "change_rate": 2.5,
            "acc_trade_price_24h": 500000000000,
        },
        {
            "market": "KRW-ETH",
            "korean_name": "이더리움",
            "english_name": "Ethereum",
            "change_rate": -1.2,
            "acc_trade_price_24h": 200000000000,
        },
    ]


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
            mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
            mcp_tools, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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
            mcp_tools, "fetch_etf_all_cached", mock_fetch_etf_all_cached
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


class TestScreenStocksCrypto:
    """Test Crypto market functionality."""

    @pytest.mark.asyncio
    async def test_crypto_default(self, mock_upbit_coins, monkeypatch):
        """Test crypto screening with default parameters."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            mcp_tools.upbit_service,
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
            mcp_tools.upbit_service,
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
            mcp_tools.upbit_service,
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


class TestScreenStocksFilters:
    """Test filter application."""

    @pytest.mark.asyncio
    async def test_kr_min_market_cap(self, mock_krx_stocks, monkeypatch):
        """Test KR market with minimum market cap filter."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
        """Test crypto market with minimum market cap filter."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            mcp_tools.upbit_service,
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
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 300000000000


class TestScreenStocksSorting:
    """Test sorting functionality."""

    @pytest.mark.asyncio
    async def test_kr_sort_by_volume_desc(self, mock_krx_stocks, monkeypatch):
        """Test KR market sorted by volume descending."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
            mcp_tools, "fetch_stock_all_cached", mock_fetch_stock_all_cached
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
