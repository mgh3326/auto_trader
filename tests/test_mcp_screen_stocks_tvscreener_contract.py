from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling import analysis_screen_core
from app.mcp_server.tooling.screening import enrichment as screening_enrichment
from app.mcp_server.tooling.screening import us as screening_us
from app.services.tvscreener_service import (
    TvScreenerCapabilitySnapshot,
    TvScreenerCapabilityState,
)
from tests._mcp_tooling_support import build_tools

pytest_plugins = ("tests._mcp_tooling_support",)


@pytest.fixture(autouse=True)
def _disable_kr_ranking_snapshot():
    """ROB-388: these contract tests assert the *tvscreener* KR path specifically. The
    snapshot-primary path (kr_market_ranking) now precedes tvscreener for plain eligible
    KR requests and is covered in test_mcp_screen_stocks_kr.py, so disable it here to keep
    exercising the tvscreener contract in isolation."""
    with patch(
        "app.mcp_server.tooling.screening.kr.load_kr_ranking_snapshot",
        new=AsyncMock(return_value=None),
    ):
        yield


def _stock_capability_snapshot(
    market: str,
    **statuses: TvScreenerCapabilityState,
) -> TvScreenerCapabilitySnapshot:
    return TvScreenerCapabilitySnapshot(
        screener="stock",
        market=market,
        statuses=statuses,
        fields={
            name: name if state is TvScreenerCapabilityState.USABLE else None
            for name, state in statuses.items()
        },
    )


def _install_stock_capabilities(
    monkeypatch,
    *,
    overrides: dict[str, TvScreenerCapabilityState] | None = None,
) -> None:
    capability_overrides = dict(overrides or {})

    async def mock_get_stock_capabilities(self, *, market, capability_names):
        del self
        normalized_market = "kr" if market in {"kr", "kospi", "kosdaq"} else market
        statuses = {
            name: capability_overrides.get(name, TvScreenerCapabilityState.USABLE)
            for name in capability_names
        }
        return _stock_capability_snapshot(normalized_market, **statuses)

    monkeypatch.setattr(
        "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
        mock_get_stock_capabilities,
        raising=False,
    )


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
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["close"] == pytest.approx(70000.0)
        assert result["results"][0]["change_rate"] == pytest.approx(2.5)
        assert result["results"][0]["market"] == "KOSPI"
        assert result["results"][0]["market_cap"] == 4800000
        assert result["results"][0]["per"] == pytest.approx(12.5)
        assert result["results"][0]["pbr"] == pytest.approx(1.2)
        assert result["results"][0]["dividend_yield"] == pytest.approx(0.0256)
        assert result["results"][0]["adx"] == pytest.approx(24.8)
        assert result["filters_applied"]["sort_order"] == "desc"
        assert result["filters_applied"]["min_market_cap"] == 300000
        assert result["filters_applied"]["max_per"] == pytest.approx(15.0)
        assert result["filters_applied"]["max_pbr"] == pytest.approx(2.0)
        assert result["filters_applied"]["min_dividend_yield"] == pytest.approx(0.02)
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
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["close"] == pytest.approx(175.5)
        assert result["results"][0]["change_rate"] == pytest.approx(1.2)
        assert result["results"][0]["market"] == "us"
        assert result["results"][0]["market_cap"] == 2800000000000
        assert result["results"][0]["per"] == pytest.approx(28.5)
        assert result["results"][0]["dividend_yield"] == pytest.approx(0.005)
        assert result["results"][0]["adx"] == pytest.approx(31.4)
        assert result["filters_applied"]["sort_order"] == "asc"
        assert result["filters_applied"]["min_market_cap"] == 1000000000
        assert result["filters_applied"]["max_per"] == pytest.approx(30.0)
        assert result["filters_applied"]["min_dividend_yield"] == pytest.approx(0.004)
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
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            fail_legacy_kr,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["rsi"] == pytest.approx(41.2)
        assert result["results"][0]["adx"] == pytest.approx(23.5)
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
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["adx"] == pytest.approx(31.4)

    @pytest.mark.asyncio
    async def test_kr_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["max_rsi"] == pytest.approx(35.0)
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
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            fail_legacy_kr,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["rsi"] == pytest.approx(32.0)
        assert result["results"][0]["adx"] == pytest.approx(21.5)

    @pytest.mark.asyncio
    async def test_us_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["max_rsi"] == pytest.approx(40.0)
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
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )
        _install_stock_capabilities(monkeypatch)

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
        assert result["results"][0]["rsi"] == pytest.approx(35.2)
        assert result["results"][0]["adx"] == pytest.approx(31.4)

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
            assert kwargs["max_rsi"] == pytest.approx(40.0)
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
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            mock_screen_us,
        )
        _install_stock_capabilities(monkeypatch)

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
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        _install_stock_capabilities(monkeypatch)

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
    @pytest.mark.asyncio
    async def test_us_sector_request_uses_tvscreener_when_capability_verified(
        self, monkeypatch
    ):
        async def mock_get_stock_capabilities(self, *, market, capability_names):
            assert market == "us"
            assert "sector" in capability_names
            return _stock_capability_snapshot(
                market,
                volume=TvScreenerCapabilityState.USABLE,
                change_rate=TvScreenerCapabilityState.USABLE,
                rsi=TvScreenerCapabilityState.USABLE,
                adx=TvScreenerCapabilityState.USABLE,
                sector=TvScreenerCapabilityState.USABLE,
            )

        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["category"] == "Technology"
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75_000_000.0,
                        "sector": "Technology",
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError("legacy US path should not run when sector is usable")

        monkeypatch.setattr(
            "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
            mock_get_stock_capabilities,
            raising=False,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["sector"] == "Technology"

    @pytest.mark.asyncio
    async def test_us_sector_request_falls_back_to_legacy_when_capability_missing(
        self, monkeypatch
    ):
        async def mock_get_stock_capabilities(self, *, market, capability_names):
            assert market == "us"
            assert "sector" in capability_names
            return _stock_capability_snapshot(
                market,
                volume=TvScreenerCapabilityState.USABLE,
                change_rate=TvScreenerCapabilityState.USABLE,
                rsi=TvScreenerCapabilityState.USABLE,
                adx=TvScreenerCapabilityState.USABLE,
                sector=TvScreenerCapabilityState.UNSUPPORTED,
            )

        async def fail_tvscreener_us(**kwargs):
            raise AssertionError(
                "tvscreener US path should not run when sector capability is unsupported"
            )

        async def mock_screen_us(**kwargs):
            return {
                "results": [
                    {
                        "code": "AAPL",
                        "name": "Apple Inc.",
                        "close": 175.5,
                        "change_rate": 1.2,
                        "volume": 75_000_000.0,
                        "sector": "Technology",
                        "market": "us",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "us",
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "market": "us",
                "timestamp": "2026-03-07T00:00:00+00:00",
                "meta": {"source": "legacy"},
            }

        monkeypatch.setattr(
            "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
            mock_get_stock_capabilities,
            raising=False,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            fail_tvscreener_us,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            mock_screen_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "legacy"
        assert result["results"][0]["code"] == "AAPL"

    @pytest.mark.asyncio
    async def test_us_sector_request_falls_back_to_legacy_when_capability_unknown(
        self, monkeypatch
    ):
        async def mock_get_stock_capabilities(self, *, market, capability_names):
            assert market == "us"
            assert "sector" in capability_names
            return _stock_capability_snapshot(
                market,
                volume=TvScreenerCapabilityState.USABLE,
                change_rate=TvScreenerCapabilityState.USABLE,
                rsi=TvScreenerCapabilityState.USABLE,
                adx=TvScreenerCapabilityState.USABLE,
                sector=TvScreenerCapabilityState.UNKNOWN,
            )

        async def fail_tvscreener_us(**kwargs):
            raise AssertionError(
                "tvscreener US path should not run when sector capability is unknown"
            )

        async def mock_screen_us(**kwargs):
            return {
                "results": [
                    {
                        "code": "AAPL",
                        "name": "Apple Inc.",
                        "close": 175.5,
                        "change_rate": 1.2,
                        "volume": 75_000_000.0,
                        "sector": "Technology",
                        "market": "us",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "us",
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "market": "us",
                "timestamp": "2026-03-07T00:00:00+00:00",
                "meta": {"source": "legacy"},
            }

        monkeypatch.setattr(
            "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
            mock_get_stock_capabilities,
            raising=False,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            fail_tvscreener_us,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            mock_screen_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "legacy"
        assert result["results"][0]["code"] == "AAPL"

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
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            fail_if_called,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
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

    @pytest.mark.asyncio
    async def test_kr_default_stock_request_uses_tvscreener_when_capabilities_verified(
        self, monkeypatch
    ):
        async def mock_get_stock_capabilities(self, *, market, capability_names):
            assert market == "kr"
            assert {"volume", "change_rate", "rsi", "adx"}.issubset(capability_names)
            return _stock_capability_snapshot(
                market,
                volume=TvScreenerCapabilityState.USABLE,
                change_rate=TvScreenerCapabilityState.USABLE,
                rsi=TvScreenerCapabilityState.USABLE,
                adx=TvScreenerCapabilityState.USABLE,
            )

        async def mock_screen_kr_via_tvscreener(**kwargs):
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 15_000_000.0,
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
            raise AssertionError("legacy KR path should not run when capabilities pass")

        monkeypatch.setattr(
            "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
            mock_get_stock_capabilities,
            raising=False,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
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
        assert result["results"][0]["adx"] == pytest.approx(23.5)

    @pytest.mark.asyncio
    async def test_kr_request_falls_back_to_legacy_when_capability_unverified(
        self, monkeypatch
    ):
        async def mock_get_stock_capabilities(self, *, market, capability_names):
            assert market == "kr"
            return _stock_capability_snapshot(
                market,
                volume=TvScreenerCapabilityState.UNKNOWN,
                change_rate=TvScreenerCapabilityState.USABLE,
                rsi=TvScreenerCapabilityState.USABLE,
                adx=TvScreenerCapabilityState.USABLE,
            )

        async def fail_tvscreener_kr(**kwargs):
            raise AssertionError(
                "tvscreener KR path should not run when a required capability is unknown"
            )

        async def mock_screen_kr(**kwargs):
            return {
                "results": [
                    {
                        "code": "005930",
                        "name": "삼성전자",
                        "close": 70000.0,
                        "change_rate": 2.5,
                        "volume": 15_000_000.0,
                        "market": "KOSPI",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "kr",
                    "asset_type": "stock",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "market": "kr",
                "meta": {"source": "legacy", "rsi_enrichment": {"error_samples": []}},
                "timestamp": "2026-03-07T00:00:00+00:00",
            }

        monkeypatch.setattr(
            "app.services.tvscreener_service.TvScreenerService.get_stock_capabilities",
            mock_get_stock_capabilities,
            raising=False,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            fail_tvscreener_kr,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            mock_screen_kr,
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

        assert result["meta"]["source"] == "legacy"
        assert result["results"][0]["code"] == "005930"

    @pytest.mark.asyncio
    async def test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 174.4,
                        "change_percent": 2.1,
                        "volume": 44_000_000.0,
                        "market_cap": 4_200_000.0,
                        "per": 61.3,
                        "pbr": 18.7,
                        "dividend_yield": 0.004,
                        "market": "KOSPI",
                        "sector": "Electronic Technology",
                        "analyst_buy": 65,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 269.16,
                        "upside_pct": 54.33,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "kr",
                    "asset_type": "stock",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        _install_stock_capabilities(monkeypatch)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
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

        first = result["results"][0]
        assert first["sector"] == "Electronic Technology"
        assert first["analyst_buy"] == 65
        assert first["analyst_hold"] == 4
        assert first["analyst_sell"] == 1
        assert first["avg_target"] == pytest.approx(269.16)
        assert first["upside_pct"] == pytest.approx(54.33)
        assert first["market_cap"] == pytest.approx(4_200_000.0)
        assert first["per"] == pytest.approx(61.3)
        assert first["pbr"] == pytest.approx(18.7)
        assert first["dividend_yield"] == pytest.approx(0.004)

    @pytest.mark.asyncio
    async def test_us_category_and_analyst_filter_stay_on_tvscreener_without_network_enrichment(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["category"] == "Technology"
            assert kwargs["limit"] == 1
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
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 18,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 210.0,
                        "upside_pct": 19.66,
                    },
                    {
                        "symbol": "IBM",
                        "name": "IBM",
                        "price": 190.0,
                        "change_percent": 0.4,
                        "volume": 12000000.0,
                        "market_cap": 170000000000,
                        "rsi": 42.0,
                        "adx": 22.0,
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 7,
                        "analyst_hold": 8,
                        "analyst_sell": 2,
                        "avg_target": 195.0,
                        "upside_pct": 2.63,
                    },
                ],
                "count": 2,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run for category/analyst tvscreener requests"
            )

        async def fail_enrichment(symbol: str, **kwargs):
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )
        monkeypatch.setattr(
            screening_us,
            "_can_use_tvscreener_stock_path",
            lambda **kwargs: True,
        )
        monkeypatch.setattr(
            screening_us,
            "_get_tvscreener_stock_capability_snapshot",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.enrichment._fetch_screen_enrichment_us",
            fail_enrichment,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            min_analyst_buy=10,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["total_count"] == 1
        assert result["returned_count"] == 1
        assert result["filters_applied"]["category"] == "Technology"
        assert result["filters_applied"]["min_analyst_buy"] == 10
        first = result["results"][0]
        assert first["code"] == "AAPL"
        assert first["sector"] == "Technology"
        assert first["analyst_buy"] == 18
        assert first["analyst_hold"] == 4
        assert first["analyst_sell"] == 1
        assert first["avg_target"] == pytest.approx(210.0)
        assert first["upside_pct"] == pytest.approx(19.66)

    @pytest.mark.asyncio
    async def test_us_enrichment_fallback_only_runs_for_rows_missing_tvscreener_fields(
        self, monkeypatch
    ):
        fetch_enrichment = AsyncMock(
            return_value={
                "sector": "Software",
                "analyst_buy": 16,
                "analyst_hold": 5,
                "analyst_sell": 1,
                "avg_target": 470.0,
                "upside_pct": 14.63,
            }
        )
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fetch_enrichment,
        )

        (
            rows,
            warnings,
        ) = await analysis_screen_core._decorate_screen_rows_with_equity_enrichment(
            [
                {
                    "code": "AAPL",
                    "market": "us",
                    "sector": "Technology",
                    "analyst_buy": 20,
                    "analyst_hold": 3,
                    "analyst_sell": 1,
                    "avg_target": 225.0,
                    "upside_pct": 11.8,
                },
                {
                    "code": "MSFT",
                    "market": "us",
                    "sector": None,
                    "analyst_buy": None,
                    "analyst_hold": None,
                    "analyst_sell": None,
                    "avg_target": None,
                    "upside_pct": None,
                },
            ]
        )

        assert warnings == []
        assert fetch_enrichment.await_count == 1
        assert fetch_enrichment.await_args is not None
        assert fetch_enrichment.await_args.args[0] == "MSFT"
        assert rows[0]["sector"] == "Technology"
        assert rows[0]["analyst_buy"] == 20
        assert rows[0]["avg_target"] == pytest.approx(225.0)
        assert rows[1]["sector"] == "Software"
        assert rows[1]["analyst_buy"] == 16
        assert rows[1]["analyst_hold"] == 5
        assert rows[1]["analyst_sell"] == 1
        assert rows[1]["avg_target"] == pytest.approx(470.0)
        assert rows[1]["upside_pct"] == pytest.approx(14.63)

    @pytest.mark.asyncio
    async def test_us_enrichment_fallback_preserves_existing_tvscreener_values(
        self, monkeypatch
    ):
        fetch_enrichment = AsyncMock(
            return_value={
                "sector": None,
                "analyst_buy": 0,
                "analyst_hold": 0,
                "analyst_sell": 0,
                "avg_target": 220.0,
                "upside_pct": 10.0,
            }
        )
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fetch_enrichment,
        )

        (
            rows,
            warnings,
        ) = await analysis_screen_core._decorate_screen_rows_with_equity_enrichment(
            [
                {
                    "code": "AAPL",
                    "market": "us",
                    "sector": "Technology",
                    "analyst_buy": 18,
                    "analyst_hold": 4,
                    "analyst_sell": 1,
                    "avg_target": None,
                    "upside_pct": None,
                    "close": 200.0,
                }
            ]
        )

        assert warnings == []
        assert fetch_enrichment.await_count == 1
        assert rows[0]["sector"] == "Technology"
        assert rows[0]["analyst_buy"] == 18
        assert rows[0]["analyst_hold"] == 4
        assert rows[0]["analyst_sell"] == 1
        assert rows[0]["avg_target"] == pytest.approx(220.0)
        assert rows[0]["upside_pct"] == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_us_category_preserves_acronym_case_for_tvscreener_filter(
        self, monkeypatch
    ):
        captured: dict[str, Any] = {}

        async def mock_screen_us_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "AI",
                        "name": "C3.ai, Inc.",
                        "price": 30.0,
                        "change_percent": 1.5,
                        "volume": 1000.0,
                        "market_cap": 4_000_000_000.0,
                        "market": "us",
                        "sector": "AI",
                        "analyst_buy": 9,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 36.0,
                        "upside_pct": 20.0,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "AI",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy US path should not run for AI category")

        async def fail_enrichment(symbol: str, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            screening_us,
            "_screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(screening_us, "_screen_us", fail_legacy_us)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fail_enrichment,
        )
        _install_stock_capabilities(monkeypatch)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="AI",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert captured["category"] == "AI"
        assert result["filters_applied"]["category"] == "AI"
        assert result["filters_applied"]["sector"] == "AI"
        assert result["results"][0]["sector"] == "AI"

    @pytest.mark.asyncio
    async def test_us_category_lowercase_technology_canonicalized_for_tvscreener(
        self, monkeypatch
    ):
        captured: dict[str, Any] = {}

        async def mock_screen_us_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 200.0,
                        "change_percent": 0.5,
                        "volume": 50_000.0,
                        "market_cap": 3_000_000_000_000.0,
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 30,
                        "analyst_hold": 5,
                        "analyst_sell": 1,
                        "avg_target": 250.0,
                        "upside_pct": 25.0,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                "legacy US path should not run for technology category"
            )

        async def fail_enrichment(symbol: str, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            screening_us,
            "_screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(screening_us, "_screen_us", fail_legacy_us)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fail_enrichment,
        )
        _install_stock_capabilities(monkeypatch)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert captured["category"] == "Technology"
        assert result["filters_applied"]["sector"] == "Technology"
        assert result["results"][0]["sector"] == "Technology"

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
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
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
            limit=20,
        )

        assert result["market"] == "us"
        assert "results" in result


@pytest.mark.asyncio
async def test_us_tvscreener_applies_is_common_stock_authority(monkeypatch):
    """ROB-365 bug 5: the tvscreener (primary) US path corrects a yfinance/tvscreener
    ETF mistag to 'common' using the us_symbol_universe.is_common_stock authority."""
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "symbol": "NFLX",
                "name": "Netflix Inc.",
                "price": 1000.0,
                "type": "fund",  # tvscreener glitch — NFLX is common stock
                "subtype": "ETF",
            }
        ]
    )
    monkeypatch.setattr(
        screening_us,
        "_build_us_filters",
        lambda **kwargs: {"columns": [], "where_conditions": [], "Market": object()},
    )
    monkeypatch.setattr(screening_us, "_execute_us_query", AsyncMock(return_value=df))
    monkeypatch.setattr(
        screening_us,
        "get_us_common_stock_flags",
        AsyncMock(return_value={"NFLX": True}),
    )

    result = await screening_us._screen_us_via_tvscreener(market="us", limit=10)

    assert result.get("error") is None
    nflx = next(s for s in result["stocks"] if s.get("symbol") == "NFLX")
    assert nflx["instrument_type"] == "common"


@pytest.mark.asyncio
async def test_us_tvscreener_is_common_stock_lookup_is_fail_soft(monkeypatch):
    """If the is_common_stock universe lookup raises, the tvscreener path falls back
    to algorithmic classification rather than crashing (ROB-365 bug 5)."""
    import pandas as pd

    df = pd.DataFrame(
        [
            {
                "symbol": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "price": 500.0,
                "type": "fund",
                "subtype": "ETF",
            }
        ]
    )
    monkeypatch.setattr(
        screening_us,
        "_build_us_filters",
        lambda **kwargs: {"columns": [], "where_conditions": [], "Market": object()},
    )
    monkeypatch.setattr(screening_us, "_execute_us_query", AsyncMock(return_value=df))
    monkeypatch.setattr(
        screening_us,
        "get_us_common_stock_flags",
        AsyncMock(side_effect=RuntimeError("universe DB unavailable")),
    )

    result = await screening_us._screen_us_via_tvscreener(market="us", limit=10)

    assert result.get("error") is None
    spy = next(s for s in result["stocks"] if s.get("symbol") == "SPY")
    assert spy["instrument_type"] == "etf"  # algorithmic classification preserved
