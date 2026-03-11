import pytest

from tests._mcp_tooling_support import build_tools

pytest_plugins = ("tests._mcp_tooling_support",)


class TestScreenStocksTvScreenerContract:
    @pytest.mark.asyncio
    async def test_kr_tvscreener_path_preserves_public_response_contract(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["sort_by"] == "volume"
            assert kwargs["sort_order"] == "desc"
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 15000000.0,
                        "market_cap": 4800000,
                        "per": 12.5,
                        "pbr": 1.2,
                        "dividend_yield": 0.0256,
                        "rsi": 28.1,
                        "adx": 24.8,
                        "market": "KOSPI",
                    }
                ],
                "count": 3,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "limit": 20,
                    "max_rsi": 30.0,
                    "min_market_cap": 300000,
                    "max_per": 15.0,
                    "max_pbr": 2.0,
                    "min_dividend_yield": 0.02,
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=300000,
            max_per=15.0,
            max_pbr=2.0,
            min_dividend_yield=0.02,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert set(result) >= {
            "results",
            "total_count",
            "returned_count",
            "filters_applied",
            "market",
            "timestamp",
            "meta",
        }
        assert result["total_count"] == 3
        assert result["returned_count"] == 1
        assert result["results"][0]["code"] == "005930"
        assert result["results"][0]["close"] == 70000.0
        assert result["results"][0]["change_rate"] == 2.5
        assert result["results"][0]["market"] == "KOSPI"
        assert result["results"][0]["market_cap"] == 4800000
        assert result["results"][0]["per"] == 12.5
        assert result["results"][0]["pbr"] == 1.2
        assert result["results"][0]["dividend_yield"] == 0.0256
        assert result["results"][0]["adx"] == 24.8
        assert result["filters_applied"]["sort_order"] == "desc"
        assert result["filters_applied"]["min_market_cap"] == 300000
        assert result["filters_applied"]["max_per"] == 15.0
        assert result["filters_applied"]["max_pbr"] == 2.0
        assert result["filters_applied"]["min_dividend_yield"] == 0.02
        assert result["meta"]["source"] == "tvscreener"
        assert result["meta"]["rsi_enrichment"]["error_samples"] == []

    @pytest.mark.asyncio
    async def test_us_tvscreener_path_preserves_public_response_contract(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["sort_by"] == "volume"
            assert kwargs["sort_order"] == "asc"
            assert kwargs["asset_type"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2800000000000,
                        "per": 28.5,
                        "dividend_yield": 0.005,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 4,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "asc",
                    "limit": 20,
                    "max_rsi": 40.0,
                    "min_market_cap": 1000000000,
                    "max_per": 30.0,
                    "min_dividend_yield": 0.004,
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=30.0,
            min_dividend_yield=0.004,
            max_rsi=None,
            sort_by="volume",
            sort_order="asc",
            limit=20,
        )

        assert result["total_count"] == 4
        assert result["returned_count"] == 1
        assert result["results"][0]["code"] == "AAPL"
        assert result["results"][0]["close"] == 175.5
        assert result["results"][0]["change_rate"] == 1.2
        assert result["results"][0]["market"] == "us"
        assert result["results"][0]["market_cap"] == 2800000000000
        assert result["results"][0]["per"] == 28.5
        assert result["results"][0]["dividend_yield"] == 0.005
        assert result["results"][0]["adx"] == 31.4
        assert result["filters_applied"]["sort_order"] == "asc"
        assert result["filters_applied"]["min_market_cap"] == 1000000000
        assert result["filters_applied"]["max_per"] == 30.0
        assert result["filters_applied"]["min_dividend_yield"] == 0.004
        assert result["meta"]["source"] == "tvscreener"

    @pytest.mark.asyncio
    async def test_kr_default_stock_request_uses_tvscreener_without_legacy_rsi_path(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["category"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 15000000.0,
                        "market_cap": 4800000,
                        "rsi": 41.2,
                        "adx": 23.5,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs):
            raise AssertionError(
                "legacy KR path should not run for default stock requests"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr",
            fail_legacy_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
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

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 41.2
        assert result["results"][0]["adx"] == 23.5
        assert result["meta"]["rsi_enrichment"]["error_samples"] == []

    @pytest.mark.asyncio
    async def test_us_default_stock_request_uses_tvscreener_without_legacy_path(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["category"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2800000000000,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run for default stock requests"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us",
            fail_legacy_us,
        )

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
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["adx"] == 31.4

    @pytest.mark.asyncio
    async def test_kr_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["max_rsi"] == 35.0
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 1.1,
                        "volume": 12345.0,
                        "market_cap": 4_800_000,
                        "rsi": 32.0,
                        "adx": 21.5,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 35.0,
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs):
            raise AssertionError(
                "legacy KR path should not run when max_rsi is provided"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr",
            fail_legacy_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=35.0,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 32.0
        assert result["results"][0]["adx"] == 21.5

    @pytest.mark.asyncio
    async def test_us_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["max_rsi"] == 40.0
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2_800_000_000_000,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run when max_rsi is provided"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us",
            fail_legacy_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 35.2
        assert result["results"][0]["adx"] == 31.4

    @pytest.mark.asyncio
    async def test_us_tvscreener_error_falls_back_to_legacy_path(self, monkeypatch):
        async def mock_screen_us_via_tvscreener(**kwargs):
            return {
                "stocks": [],
                "count": 0,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": None,
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "source": "tvscreener",
                "error": "tvscreener PE field unavailable",
            }

        async def mock_screen_us(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["max_rsi"] == 40.0
            return {
                "results": [
                    {
                        "code": "AAPL",
                        "name": "Apple Inc.",
                        "close": 175.5,
                        "change_rate": 1.2,
                        "volume": 75000000.0,
                        "market": "us",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": None,
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "market": "us",
                "timestamp": "2026-03-07T00:00:00+00:00",
                "meta": {"source": "legacy"},
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us",
            mock_screen_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["results"][0]["code"] == "AAPL"
        assert result["market"] == "us"
        assert result["meta"]["source"] == "legacy"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("market", ["kospi", "kosdaq"])
    async def test_kr_tvscreener_path_passes_requested_submarket(
        self, monkeypatch, market
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == market
            return {
                "stocks": [
                    {
                        "symbol": "005930" if market == "kospi" else "035720",
                        "name": "stub",
                        "price": 1.0,
                        "change_percent": 0.1,
                        "volume": 100.0,
                        "market": market.upper(),
                        "rsi": 25.0,
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market=market,
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=30.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["results"][0]["market"] == market.upper()
        assert result["filters_applied"]["market"] == market

    @pytest.mark.asyncio
    async def test_us_category_with_max_rsi_falls_back_to_legacy_path(
        self, mock_yfinance_screen, monkeypatch
    ):
        import yfinance as yf

        async def fail_if_called(**kwargs):
            raise AssertionError(
                "tvscreener path should not run for market_cap sorting"
            )

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_us_via_tvscreener",
            fail_if_called,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=50,
        )

        assert result["market"] == "us"
        assert "results" in result

    @pytest.mark.asyncio
    async def test_kr_category_with_max_rsi_falls_back_to_legacy_path(
        self, monkeypatch
    ):
        async def fail_if_called(**kwargs):
            raise AssertionError(
                "tvscreener path should not run for category-based KR screening"
            )

        async def mock_screen_kr(**kwargs):
            return {
                "results": [{"code": "069500", "name": "KODEX 200", "market": "kr"}],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "kr",
                    "asset_type": "etf",
                    "category": "반도체",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "market": "kr",
                "meta": {"rsi_enrichment": {}},
                "timestamp": "2026-03-07T00:00:00+00:00",
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr_via_tvscreener",
            fail_if_called,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.analysis_screen_core._screen_kr",
            mock_screen_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category="반도체",
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=30.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["asset_type"] == "etf"
        assert result["filters_applied"]["category"] == "반도체"
