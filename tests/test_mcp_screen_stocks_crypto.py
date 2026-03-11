from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import analysis_screen_core
from tests._mcp_tooling_support import build_tools

pytest_plugins = ("tests._mcp_tooling_support",)


class TestScreenStocksCrypto:
    @pytest.mark.asyncio
    async def test_crypto_default_restores_public_contract_on_tvscreener_success(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [-0.01, -0.02, -0.31],
                "relative_strength_index_14": [45.5, 32.1, 28.2],
                "average_directional_index_14": [25.3, 18.7, 42.1],
                "volume_24h_in_usd": [156_000_000.0, 95_000_000.0, 44_000_000.0],
                "value_traded": [900_000_000_000.0, 1_200_000_000.0, 700_000_000.0],
                "market_cap": [
                    2_500_000_000_000_000.0,
                    500_000_000_000_000.0,
                    50_000_000_000_000.0,
                ],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, object]]:
            assert market_codes == ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
            return [
                {"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0},
                {"market": "KRW-ETH", "acc_trade_volume_24h": 54_321.0},
                {"market": "KRW-XRP", "acc_trade_volume_24h": 99_999.0},
            ]

        async def mock_warning_markets(*, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return {"KRW-ETH"}

        async def mock_market_cap_cache_get() -> dict[str, object]:
            return {
                "data": {
                    "BTC": {"market_cap": 3_000_000_000_000_000, "market_cap_rank": 1}
                },
                "cached": True,
                "age_seconds": 1.5,
                "stale": False,
                "error": None,
            }

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert market_type == "crypto"
            assert count == 50
            close = [100.0 + i for i in range(50)]
            volume = [1_000.0] * 49 + [1_500.0]
            return pd.DataFrame(
                {
                    "open": close,
                    "high": [value + 10.0 for value in close],
                    "low": [value - 10.0 for value in close],
                    "close": close,
                    "volume": volume,
                }
            )

        monkeypatch.setattr(
            analysis_screen_core,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_market_display_names",
            AsyncMock(
                return_value={
                    "KRW-BTC": {
                        "korean_name": "비트코인",
                        "english_name": "Bitcoin",
                    },
                    "KRW-ETH": {
                        "korean_name": "이더리움",
                        "english_name": "Ethereum",
                    },
                }
            ),
            raising=False,
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
            sort_order="desc",
            limit=1,
        )

        query_kwargs = tv_service.query_crypto_screener.await_args.kwargs
        assert query_kwargs["limit"] == 50
        assert (
            fake_crypto_tvscreener_module.CryptoField.DESCRIPTION
            in query_kwargs["columns"]
        )
        assert (
            fake_crypto_tvscreener_module.CryptoField.MARKET_CAP
            in query_kwargs["columns"]
        )
        assert result is not None
        assert result["market"] == "crypto"
        assert len(result["results"]) == 1
        assert result["filters_applied"]["sort_by"] == "rsi"
        assert result["filters_applied"]["sort_order"] == "asc"
        assert result["meta"]["source"] == "tvscreener"
        assert result["meta"]["filtered_by_warning"] == 1
        assert result["meta"]["filtered_by_crash"] == 1

        first = result["results"][0]
        assert first["symbol"] == "KRW-BTC"
        assert first["name"] == "비트코인"
        assert first["trade_amount_24h"] == 900_000_000_000.0
        assert first["volume_24h"] == 12_345.0
        assert first["market_cap"] == 3_000_000_000_000_000
        assert first["market_cap_rank"] == 1
        assert first["rsi_bucket"] == 45
        assert first["market_warning"] is None
        assert "volume_ratio" in first
        assert "candle_type" in first
        assert "plus_di" in first
        assert "minus_di" in first
        assert "volume" not in first

    @pytest.mark.asyncio
    async def test_crypto_rsi_desc_warning_and_meta_are_stable_across_public_paths(
        self,
        fake_crypto_tvscreener_module,
        monkeypatch,
    ) -> None:
        async def mock_warning_markets(*, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return set()

        async def mock_market_cap_cache_get() -> dict[str, object]:
            return {
                "data": {},
                "cached": True,
                "age_seconds": 0.5,
                "stale": False,
                "error": None,
            }

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert market_type == "crypto"
            assert count == 50
            _ = symbol
            return pd.DataFrame(
                {
                    "open": [100.0 + i for i in range(50)],
                    "high": [101.0 + i for i in range(50)],
                    "low": [99.0 + i for i in range(50)],
                    "close": [100.0 + i for i in range(50)],
                    "volume": [1_000.0] * 50,
                }
            )

        tools = build_tools()
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW"],
                "name": ["BTCKRW"],
                "description": ["Bitcoin"],
                "price": [150_000_000.0],
                "change_percent": [-1.0],
                "relative_strength_index_14": [45.0],
                "average_directional_index_14": [20.0],
                "value_traded": [900_000_000_000.0],
                "market_cap": [2_500_000_000_000_000.0],
                "volume_24h_in_usd": [1.0],
                "exchange": ["UPBIT"],
            }
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            AsyncMock(
                return_value=[{"market": "KRW-BTC", "acc_trade_volume_24h": 1.0}]
            ),
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_market_display_names",
            AsyncMock(return_value={}),
            raising=False,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )

        tv_result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=1,
        )

        monkeypatch.setattr(
            analysis_screen_core,
            "_screen_crypto_via_tvscreener",
            AsyncMock(side_effect=RuntimeError("boom")),
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            AsyncMock(
                return_value=[
                    {
                        "market": "KRW-BTC",
                        "korean_name": "비트코인",
                        "trade_price": 150_000_000.0,
                        "signed_change_rate": -0.01,
                        "acc_trade_volume_24h": 1.0,
                        "acc_trade_price_24h": 900_000_000_000.0,
                        "rsi": 45.0,
                    }
                ]
            ),
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_enrich_crypto_indicators",
            AsyncMock(
                return_value={
                    "attempted": 1,
                    "succeeded": 1,
                    "failed": 0,
                    "rate_limited": 0,
                    "timeout": 0,
                    "error_samples": [],
                }
            ),
        )

        legacy_result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=1,
        )

        expected_warning = "crypto sort_by='rsi' always uses ascending order; requested desc was ignored."
        required_meta_keys = {
            "rsi_enrichment",
            "filtered_by_warning",
            "filtered_by_crash",
            "final_count",
            "coingecko_cached",
            "coingecko_age_seconds",
        }

        assert tv_result["warnings"] == [expected_warning]
        assert legacy_result["warnings"] == [expected_warning]
        assert tv_result["filters_applied"]["sort_order"] == "asc"
        assert legacy_result["filters_applied"]["sort_order"] == "asc"
        assert required_meta_keys <= set(tv_result["meta"])
        assert required_meta_keys <= set(legacy_result["meta"])

    @pytest.mark.asyncio
    async def test_crypto_sort_by_volume_raises_error(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()
        with pytest.raises(ValueError, match=".*does not support sorting by.*volume.*"):
            await tools["screen_stocks"](
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

    @pytest.mark.asyncio
    async def test_crypto_trade_amount_sorting_uses_24h_trade_value(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            assert fiat == "KRW"
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 9_999_999,
                    "acc_trade_price_24h": 1_000,
                },
                {
                    "market": "KRW-ETH",
                    "korean_name": "이더리움",
                    "trade_price": 5_000_000,
                    "signed_change_rate": 0.02,
                    "acc_trade_volume_24h": 1,
                    "acc_trade_price_24h": 10_000,
                },
                {
                    "market": "KRW-SOL",
                    "korean_name": "솔라나",
                    "trade_price": 200_000,
                    "signed_change_rate": 0.03,
                    "acc_trade_volume_24h": 100,
                    "acc_trade_price_24h": 5_000,
                },
            ]

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
            sort_by="trade_amount",
            sort_order="desc",
            limit=3,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert symbols == ["KRW-ETH", "KRW-SOL", "KRW-BTC"]
        assert all("trade_amount_24h" in item for item in result["results"])
        assert all("volume" not in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_per_filter_raises_error(self, mock_upbit_coins, monkeypatch):
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
                sort_by="trade_amount",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_dividend_filter_raises_error(
        self, mock_upbit_coins, monkeypatch
    ):
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
                sort_by="trade_amount",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "pattern"),
        [
            ({"sector": "Layer1"}, ".*crypto.*sector.*"),
            ({"min_analyst_buy": 5}, ".*crypto.*min_analyst_buy.*"),
            ({"min_dividend": 2.0}, ".*crypto.*min_dividend.*"),
        ],
    )
    async def test_crypto_new_equity_fundamentals_filters_raise_errors(
        self,
        kwargs: dict[str, object],
        pattern: str,
    ) -> None:
        tools = build_tools()

        with pytest.raises(ValueError, match=pattern):
            await tools["screen_stocks"](market="crypto", limit=5, **kwargs)

    @pytest.mark.asyncio
    async def test_crypto_enriches_metrics_without_explicit_rsi_filters(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        enrich_mock = AsyncMock(
            return_value={
                "attempted": 2,
                "succeeded": 0,
                "failed": 2,
                "rate_limited": 0,
                "timeout": 0,
                "error_samples": [],
            }
        )

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_enrich_crypto_indicators",
            enrich_mock,
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
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        enrich_mock.assert_awaited_once()
        assert result["meta"]["rsi_enrichment"]["attempted"] > 0
        assert all("score" not in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_screen_crypto_uses_batch_realtime_rsi_engine(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        async def enrich_candidates(candidates):
            candidates[0]["rsi"] = 41.0
            candidates[0]["rsi_bucket"] = 40
            candidates[1]["rsi"] = 29.0
            candidates[1]["rsi_bucket"] = 25
            return {
                "attempted": 2,
                "succeeded": 2,
                "failed": 0,
                "rate_limited": 0,
                "timeout": 0,
                "error_samples": [],
            }

        enrich_mock = AsyncMock(side_effect=enrich_candidates)

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_enrich_crypto_indicators",
            enrich_mock,
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
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )

        enrich_mock.assert_awaited_once()
        assert result["meta"]["rsi_enrichment"]["attempted"] == 2
        assert result["meta"]["rsi_enrichment"]["succeeded"] == 2
        assert all("rsi" in item for item in result["results"])
        assert all("rsi_14" not in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_sort_by_rsi_desc_forces_asc_with_warning(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:AKRW", "UPBIT:BKRW", "UPBIT:CKRW"],
                "name": ["AKRW", "BKRW", "CKRW"],
                "description": ["A coin", "B coin", "C coin"],
                "price": [1_000.0, 1_000.0, 1_000.0],
                "change_percent": [-0.01, -0.02, -0.03],
                "relative_strength_index_14": [24.0, 22.0, 27.0],
                "average_directional_index_14": [20.0, 20.0, 20.0],
                "value_traded": [100.0, 300.0, 1_000.0],
                "market_cap": [10.0, 20.0, 30.0],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, object]]:
            return [
                {"market": code, "acc_trade_volume_24h": 1.0} for code in market_codes
            ]

        async def mock_warning_markets(*, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return set()

        async def mock_market_cap_cache_get() -> dict[str, object]:
            return {
                "data": {},
                "cached": True,
                "age_seconds": 0.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            analysis_screen_core,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
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
            sort_by="rsi",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["sort_order"] == "asc"
        assert any("requested desc was ignored" in w for w in result["warnings"])
        assert [item["symbol"] for item in result["results"]] == [
            "KRW-B",
            "KRW-A",
            "KRW-C",
        ]

    @pytest.mark.asyncio
    async def test_crypto_market_cap_sort_uses_public_market_cap(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["BTC TV", "ETH TV", "XRP TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [1.0, 1.0, 1.0],
                "relative_strength_index_14": [45.5, 32.1, 68.9],
                "average_directional_index_14": [25.3, 18.7, 42.1],
                "value_traded": [9_000.0, 1_000.0, 2_000.0],
                "market_cap": [20.0, 10.0, 50.0],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, object]]:
            return [
                {"market": code, "acc_trade_volume_24h": 1.0} for code in market_codes
            ]

        async def mock_warning_markets(*, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return set()

        async def mock_market_cap_cache_get() -> dict[str, object]:
            return {
                "data": {"ETH": {"market_cap": 100.0, "market_cap_rank": 2}},
                "cached": True,
                "age_seconds": 0.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            analysis_screen_core,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
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
            sort_by="market_cap",
            sort_order="desc",
            limit=3,
        )

        assert tv_service.query_crypto_screener.await_args.kwargs["sort_by"] == (
            fake_crypto_tvscreener_module.CryptoField.MARKET_CAP
        )
        assert [item["symbol"] for item in result["results"]] == [
            "KRW-ETH",
            "KRW-XRP",
            "KRW-BTC",
        ]
        assert [item["market_cap"] for item in result["results"]] == [100.0, 50.0, 20.0]

    @pytest.mark.asyncio
    async def test_crypto_market_warning_filter_counts(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.02,
                    "acc_trade_price_24h": 1_000_000_000_000,
                },
                {
                    "market": "KRW-ETH",
                    "korean_name": "이더리움",
                    "trade_price": 5_000_000,
                    "signed_change_rate": -0.01,
                    "acc_trade_price_24h": 800_000_000_000,
                },
            ]

        warning_markets_mock = AsyncMock(return_value={"KRW-ETH"})

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "get_upbit_warning_markets",
            warning_markets_mock,
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
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        symbols = [item["symbol"] for item in result["results"]]
        warning_markets_mock.assert_awaited_once_with(quote_currency="KRW")
        assert "KRW-ETH" not in symbols
        assert result["meta"]["filtered_by_warning"] == 1
        assert all(item["market_warning"] is None for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_crash_filter_applies_isolated_drop(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.05,
                    "acc_trade_price_24h": 1_000_000_000_000,
                },
                {
                    "market": "KRW-AAA",
                    "korean_name": "에이에이",
                    "trade_price": 1_000,
                    "signed_change_rate": -0.31,
                    "acc_trade_price_24h": 900_000_000,
                },
            ]

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
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert "KRW-AAA" not in symbols
        assert result["meta"]["filtered_by_crash"] == 1

    @pytest.mark.asyncio
    async def test_crypto_crash_filter_allows_market_panic(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.12,
                    "acc_trade_price_24h": 1_000_000_000_000,
                },
                {
                    "market": "KRW-AAA",
                    "korean_name": "에이에이",
                    "trade_price": 1_000,
                    "signed_change_rate": -0.31,
                    "acc_trade_price_24h": 900_000_000,
                },
            ]

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
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert "KRW-AAA" in symbols
        assert result["meta"]["filtered_by_crash"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_bucket_sort_tiebreaks_with_trade_amount(
        self, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-A",
                    "trade_price": 1_000,
                    "signed_change_rate": -0.01,
                    "acc_trade_price_24h": 100,
                    "rsi": 24.0,
                },
                {
                    "market": "KRW-B",
                    "trade_price": 1_000,
                    "signed_change_rate": -0.02,
                    "acc_trade_price_24h": 300,
                    "rsi": 22.0,
                },
                {
                    "market": "KRW-C",
                    "trade_price": 1_000,
                    "signed_change_rate": -0.03,
                    "acc_trade_price_24h": 1_000,
                    "rsi": 27.0,
                },
            ]

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
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert symbols == ["KRW-B", "KRW-A", "KRW-C"]
        buckets = [item["rsi_bucket"] for item in result["results"]]
        assert buckets == [20, 20, 25]

    @pytest.mark.asyncio
    async def test_crypto_coingecko_enrichment_sets_market_cap_and_meta(
        self, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.01,
                    "acc_trade_price_24h": 1_000_000_000_000,
                    "rsi": 32.0,
                }
            ]

        async def mock_market_cap_cache_get():
            return {
                "data": {
                    "BTC": {"market_cap": 2_000_000_000_000_000, "market_cap_rank": 1}
                },
                "cached": True,
                "age_seconds": 2.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
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
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )

        first = result["results"][0]
        assert first["market_cap"] == 2_000_000_000_000_000
        assert first["market_cap_rank"] == 1
        assert result["meta"]["coingecko_cached"] is True
        assert result["meta"]["coingecko_age_seconds"] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_crypto_coingecko_stale_fallback_adds_warning(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.01,
                    "acc_trade_price_24h": 1_000_000_000_000,
                    "rsi": 30.0,
                }
            ]

        async def mock_market_cap_cache_get():
            return {
                "data": {
                    "BTC": {"market_cap": 1_900_000_000_000_000, "market_cap_rank": 1}
                },
                "cached": True,
                "age_seconds": 1200.0,
                "stale": True,
                "error": "TimeoutError: boom",
            }

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        monkeypatch.setattr(
            analysis_screen_core._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
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
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )

        assert result["results"][0]["market_cap"] == 1_900_000_000_000_000
        assert any("stale cache was used" in w for w in result["warnings"])
