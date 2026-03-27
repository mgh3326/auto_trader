import asyncio
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import sentry_sdk

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling.screening import crypto as screening_crypto
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

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
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
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
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
    async def test_crypto_enrichment_runs_in_parallel(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [-0.12, -0.02, -0.31],
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

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            assert db is mock_session
            return set()

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

        current_concurrent = 0
        max_concurrent = 0

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            nonlocal current_concurrent, max_concurrent
            assert symbol in {"KRW-BTC", "KRW-ETH", "KRW-XRP"}
            assert market_type == "crypto"
            assert count == 50
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            try:
                await asyncio.sleep(0.05)
            finally:
                current_concurrent -= 1
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

        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
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
                    "KRW-XRP": {
                        "korean_name": "리플",
                        "english_name": "Ripple",
                    },
                }
            ),
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
            "AsyncSessionLocal",
            lambda: mock_session,
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
            limit=3,
        )

        assert result["returned_count"] == 3
        assert max_concurrent > 1
        assert max_concurrent <= 5

    @pytest.mark.asyncio
    async def test_crypto_coingecko_overlaps_with_enrichment(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [-0.01, -0.02, -0.03],
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

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            assert db is mock_session
            return set()

        coingecko_started = asyncio.Event()
        enrichment_done = asyncio.Event()

        async def mock_market_cap_cache_get() -> dict[str, object]:
            coingecko_started.set()
            await enrichment_done.wait()
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
            assert symbol == "KRW-BTC"
            assert market_type == "crypto"
            assert count == 50
            await asyncio.wait_for(coingecko_started.wait(), timeout=0.5)
            close = [100.0 + i for i in range(50)]
            volume = [1_000.0] * 49 + [1_500.0]
            enrichment_done.set()
            return pd.DataFrame(
                {
                    "open": close,
                    "high": [value + 10.0 for value in close],
                    "low": [value - 10.0 for value in close],
                    "close": close,
                    "volume": volume,
                }
            )

        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
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
                    "KRW-XRP": {
                        "korean_name": "리플",
                        "english_name": "Ripple",
                    },
                }
            ),
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
            "AsyncSessionLocal",
            lambda: mock_session,
        )

        tools = build_tools()
        await tools["screen_stocks"](
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

        assert coingecko_started.is_set()

    @pytest.mark.asyncio
    async def test_crypto_cancellation_does_not_leak_coingecko_task(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW"],
                "name": ["BTCKRW"],
                "description": ["Bitcoin TV"],
                "price": [150_000_000.0],
                "change_percent": [-0.01],
                "relative_strength_index_14": [45.5],
                "average_directional_index_14": [25.3],
                "volume_24h_in_usd": [156_000_000.0],
                "value_traded": [900_000_000_000.0],
                "market_cap": [2_500_000_000_000_000.0],
                "exchange": ["UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, object]]:
            assert market_codes == ["KRW-BTC"]
            return [{"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0}]

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            assert db is mock_session
            return set()

        coingecko_started = asyncio.Event()
        enrichment_started = asyncio.Event()
        allow_exit = asyncio.Event()
        coingecko_cancelled = False

        async def mock_market_cap_cache_get() -> dict[str, object]:
            nonlocal coingecko_cancelled
            coingecko_started.set()
            try:
                await allow_exit.wait()
            except asyncio.CancelledError:
                coingecko_cancelled = True
                raise
            return {
                "data": {},
                "cached": False,
                "age_seconds": None,
                "stale": False,
                "error": None,
            }

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert symbol == "KRW-BTC"
            assert market_type == "crypto"
            assert count == 50
            enrichment_started.set()
            await allow_exit.wait()
            raise AssertionError("enrichment should be cancelled before completion")

        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_market_display_names",
            AsyncMock(
                return_value={
                    "KRW-BTC": {
                        "korean_name": "비트코인",
                        "english_name": "Bitcoin",
                    }
                }
            ),
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
            "AsyncSessionLocal",
            lambda: mock_session,
        )

        tools = build_tools()
        screen_task = asyncio.create_task(
            tools["screen_stocks"](
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
        )

        try:
            await asyncio.wait_for(coingecko_started.wait(), timeout=0.5)
            await asyncio.wait_for(enrichment_started.wait(), timeout=0.5)
            screen_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await screen_task
            assert coingecko_cancelled is True
        finally:
            allow_exit.set()

    @pytest.mark.asyncio
    async def test_crypto_coingecko_span_wraps_actual_fetch(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        active_spans: list[tuple[str, str, object]] = []

        class _DummySpan:
            def __init__(self) -> None:
                self.data: dict[str, object] = {}

            def set_data(self, key: str, value: object) -> None:
                self.data[key] = value

        class _DummySpanContext:
            def __init__(self, op: str, name: str, span: _DummySpan) -> None:
                self._op = op
                self._name = name
                self._span = span

            def __enter__(self) -> _DummySpan:
                active_spans.append((self._op, self._name, self._span))
                return self._span

            def __exit__(self, exc_type, exc, tb) -> bool:
                active_spans.pop()
                return False

        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW"],
                "name": ["BTCKRW"],
                "description": ["Bitcoin TV"],
                "price": [150_000_000.0],
                "change_percent": [-0.01],
                "relative_strength_index_14": [45.5],
                "average_directional_index_14": [25.3],
                "volume_24h_in_usd": [156_000_000.0],
                "value_traded": [900_000_000_000.0],
                "market_cap": [2_500_000_000_000_000.0],
                "exchange": ["UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, object]]:
            assert market_codes == ["KRW-BTC"]
            return [{"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0}]

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            assert db is mock_session
            return set()

        async def mock_market_cap_cache_get() -> dict[str, object]:
            assert active_spans
            assert active_spans[-1][0] == "crypto.screen.coingecko"
            assert active_spans[-1][1] == "crypto coingecko fetch"
            return {
                "data": {
                    "BTC": {
                        "market_cap": 3_000_000_000_000_000,
                        "market_cap_rank": 1,
                    }
                },
                "cached": True,
                "age_seconds": 1.5,
                "stale": True,
                "error": "TimeoutError: stale cache used",
            }

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert symbol == "KRW-BTC"
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

        started: list[tuple[str, str, _DummySpan]] = []

        def fake_start_span(op: str, name: str) -> _DummySpanContext:
            span = _DummySpan()
            started.append((op, name, span))
            return _DummySpanContext(op, name, span)

        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        monkeypatch.setattr(sentry_sdk, "start_span", fake_start_span)
        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_market_display_names",
            AsyncMock(
                return_value={
                    "KRW-BTC": {
                        "korean_name": "비트코인",
                        "english_name": "Bitcoin",
                    }
                }
            ),
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
            "AsyncSessionLocal",
            lambda: mock_session,
        )

        tools = build_tools()
        await tools["screen_stocks"](
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

        coingecko_span = next(
            span_info
            for span_info in started
            if span_info[0] == "crypto.screen.coingecko"
        )
        assert coingecko_span[1] == "crypto coingecko fetch"
        assert coingecko_span[2].data["coingecko_cached"] is True
        assert coingecko_span[2].data["coingecko_stale"] is True
        assert coingecko_span[2].data["coingecko_error_present"] is True

    @pytest.mark.asyncio
    async def test_crypto_db_session_reuse_for_display_names_and_warnings(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [-0.01, -0.02, -0.03],
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

        display_name_dbs: list[object] = []
        warning_dbs: list[object] = []

        async def mock_get_upbit_market_display_names(
            market_codes: list[str], db=None
        ) -> dict[str, dict[str, str]]:
            assert market_codes == ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
            display_name_dbs.append(db)
            return {
                "KRW-BTC": {"korean_name": "비트코인", "english_name": "Bitcoin"},
                "KRW-ETH": {"korean_name": "이더리움", "english_name": "Ethereum"},
                "KRW-XRP": {"korean_name": "리플", "english_name": "Ripple"},
            }

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            warning_dbs.append(db)
            return set()

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
            assert symbol == "KRW-BTC"
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

        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_market_display_names",
            mock_get_upbit_market_display_names,
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
            "AsyncSessionLocal",
            lambda: mock_session,
        )

        tools = build_tools()
        await tools["screen_stocks"](
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

        assert len(display_name_dbs) == 1
        assert len(warning_dbs) == 1
        assert display_name_dbs[0] is mock_session
        assert warning_dbs[0] is mock_session
        assert display_name_dbs[0] is warning_dbs[0]

    @pytest.mark.asyncio
    async def test_crypto_rsi_desc_warning_and_meta_are_stable(
        self,
        fake_crypto_tvscreener_module,
        monkeypatch,
    ) -> None:
        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
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
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
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
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_market_display_names",
            AsyncMock(return_value={}),
            raising=False,
        )
        monkeypatch.setattr(
            screening_crypto,
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
        assert tv_result["filters_applied"]["sort_order"] == "asc"
        assert required_meta_keys <= set(tv_result["meta"])

    @pytest.mark.asyncio
    async def test_crypto_error_propagates_without_fallback(
        self, mock_upbit_coins, monkeypatch
    ):
        """tvscreener 실패 시 legacy fallback 없이 예외가 전파되어야 함."""
        tools = build_tools()

        monkeypatch.setattr(
            screening_crypto,
            "_screen_crypto_via_tvscreener",
            AsyncMock(side_effect=RuntimeError("tvscreener boom")),
        )

        with pytest.raises(RuntimeError, match="tvscreener boom"):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=1,
            )

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

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
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
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
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

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
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
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
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
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.02,
                    "trade_amount_24h": 1_000_000_000_000,
                    "market_warning": None,
                }
            ],
            "meta": {
                "filtered_by_warning": 1,
                "filtered_by_crash": 0,
            },
            "warnings": [],
            "filters_applied": {
                "sort_by": "trade_amount",
                "sort_order": "desc",
            },
            "market": "crypto",
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
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert "KRW-ETH" not in symbols
        assert result["meta"]["filtered_by_warning"] == 1
        assert all(item["market_warning"] is None for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_crash_filter_applies_isolated_drop(self, monkeypatch):
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.05,
                    "trade_amount_24h": 1_000_000_000_000,
                }
            ],
            "meta": {
                "filtered_by_crash": 1,
            },
            "warnings": [],
            "filters_applied": {},
            "market": "crypto",
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
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        assert result["meta"]["filtered_by_crash"] == 1

    @pytest.mark.asyncio
    async def test_crypto_crash_filter_allows_market_panic(self, monkeypatch):
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "name": "비트코인",
                    "signed_change_rate": -0.12,
                },
                {
                    "symbol": "KRW-AAA",
                    "name": "에이에이",
                    "signed_change_rate": -0.31,
                },
            ],
            "meta": {
                "filtered_by_crash": 0,
            },
            "warnings": [],
            "filters_applied": {},
            "market": "crypto",
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
    async def test_crypto_coingecko_enrichment_sets_market_cap_and_meta(self, monkeypatch):
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.01,
                    "trade_amount_24h": 1_000_000_000_000,
                    "rsi": 32.0,
                    "market_cap": 2_000_000_000_000_000,
                    "market_cap_rank": 1,
                }
            ],
            "meta": {
                "coingecko_cached": True,
                "coingecko_age_seconds": 2.0,
            },
            "warnings": [],
            "filters_applied": {
                "sort_by": "rsi",
                "sort_order": "asc",
            },
            "market": "crypto",
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
    async def test_crypto_large_ticker_enrichment_uses_real_batched_client_path(
        self,
        fake_crypto_tvscreener_module,
        monkeypatch,
    ) -> None:
        requested_batches: list[list[str]] = []
        candidate_count = 120
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": [
                    f"UPBIT:COIN{index:03d}KRW" for index in range(candidate_count)
                ],
                "name": [f"COIN{index:03d}KRW" for index in range(candidate_count)],
                "description": [
                    f"Coin {index:03d}" for index in range(candidate_count)
                ],
                "price": [1_000.0 + index for index in range(candidate_count)],
                "change_percent": [-0.01 for _ in range(candidate_count)],
                "relative_strength_index_14": [40.0 for _ in range(candidate_count)],
                "average_directional_index_14": [20.0 for _ in range(candidate_count)],
                "volume_24h_in_usd": [10_000.0 for _ in range(candidate_count)],
                "value_traded": [
                    1_000_000_000.0 - index for index in range(candidate_count)
                ],
                "market_cap": [100_000_000_000.0 for _ in range(candidate_count)],
                "exchange": ["UPBIT" for _ in range(candidate_count)],
            }
        )

        async def fake_request_json(url: str, params=None):
            assert params is None
            batch_codes = parse_qs(urlparse(url).query)["markets"][0].split(",")
            requested_batches.append(batch_codes)
            return [
                {
                    "market": market_code,
                    "acc_trade_volume_24h": float(index + 1),
                }
                for index, market_code in enumerate(batch_codes)
            ]

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert symbol.startswith("KRW-COIN")
            assert market_type == "crypto"
            assert count == 50
            close = [100.0 + i for i in range(50)]
            return pd.DataFrame(
                {
                    "open": close,
                    "high": [value + 10.0 for value in close],
                    "low": [value - 10.0 for value in close],
                    "close": close,
                    "volume": [1_000.0] * 49 + [1_500.0],
                }
            )

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            screening_crypto,
            "_fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(upbit_service, "_request_json", fake_request_json)
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            AsyncMock(
                return_value={
                    "data": {},
                    "cached": False,
                    "age_seconds": None,
                    "stale": False,
                    "error": None,
                }
            ),
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
            limit=25,
        )

        query_kwargs = tv_service.query_crypto_screener.await_args.kwargs
        assert query_kwargs["limit"] == 125
        assert [len(batch) for batch in requested_batches] == [50, 50, 20]
        assert len(result["results"]) == 25
        assert all(item["volume_24h"] > 0.0 for item in result["results"])
        assert not any(
            "volume_24h defaulted to 0.0" in warning for warning in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_crypto_coingecko_stale_fallback_adds_warning(self, monkeypatch):
        mock_result = {
            "results": [
                {
                    "symbol": "KRW-BTC",
                    "trade_price": 100_000_000,
                    "signed_change_rate": -0.01,
                    "trade_amount_24h": 1_000_000_000_000,
                    "rsi": 30.0,
                    "market_cap": 1_900_000_000_000_000,
                }
            ],
            "meta": {},
            "warnings": ["stale cache was used"],
            "filters_applied": {
                "sort_by": "rsi",
                "sort_order": "asc",
            },
            "market": "crypto",
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


# ------------------------------------------------------------------------------
# Stop-loss cooldown filter tests
# ------------------------------------------------------------------------------


class FakeCooldownService:
    """Fake cooldown service for testing."""

    def __init__(self, blocked: set[str] | None = None, in_cooldown: bool = False):
        self._blocked = blocked or set()
        self._in_cooldown = in_cooldown

    async def is_in_cooldown(self, symbol: str) -> bool:
        if self._in_cooldown:
            return True
        return symbol.upper() in self._blocked

    async def filter_symbols_in_cooldown(self, symbols: list[str]) -> set[str]:
        return {
            symbol.upper().strip()
            for symbol in symbols
            if symbol and (self._in_cooldown or symbol.upper().strip() in self._blocked)
        }

    async def record_stop_loss(self, symbol: str) -> None:
        pass

    async def get_remaining_ttl_seconds(self, symbol: str) -> int | None:
        if symbol.upper() in self._blocked:
            return 86400
        return None


@pytest.mark.asyncio
async def test_screen_stocks_crypto_filters_stop_loss_cooldown_symbols(
    fake_crypto_tvscreener_module, monkeypatch
):
    """Test that cooled-down symbols are filtered from crypto screening results."""
    tv_service = AsyncMock()
    tv_service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
            "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
            "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
            "price": [150_000_000.0, 5_000_000.0, 3_000.0],
            "change_percent": [
                -0.01,
                -0.02,
                -0.015,
            ],  # Similar drops to avoid crash filter
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
        return [
            {"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0},
            {"market": "KRW-ETH", "acc_trade_volume_24h": 54_321.0},
            {"market": "KRW-XRP", "acc_trade_volume_24h": 99_999.0},
        ]

    async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
        return set()

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    monkeypatch.setattr(
        screening_crypto,
        "_import_tvscreener",
        lambda: fake_crypto_tvscreener_module,
    )
    monkeypatch.setattr(
        screening_crypto,
        "TvScreenerService",
        lambda timeout=30.0: tv_service,
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_tickers",
        mock_fetch_multiple_tickers,
    )
    monkeypatch.setattr(
        screening_crypto,
        "get_upbit_warning_markets",
        mock_warning_markets,
        raising=False,
    )
    monkeypatch.setattr(
        screening_crypto,
        "AsyncSessionLocal",
        lambda: mock_session,
    )
    # Inject fake cooldown service that blocks KRW-BTC
    monkeypatch.setattr(
        screening_crypto,
        "_get_crypto_trade_cooldown_service",
        lambda: FakeCooldownService(blocked={"KRW-BTC"}),
    )

    tools = build_tools()
    result = await tools["screen_stocks"](
        market="crypto",
        limit=5,
    )

    symbols = [item["symbol"] for item in result["results"]]
    assert "KRW-BTC" not in symbols
    assert "KRW-ETH" in symbols
    assert "KRW-XRP" in symbols
    assert result["meta"]["filtered_by_stop_loss_cooldown"] == 1


@pytest.mark.asyncio
async def test_screen_stocks_crypto_cooldown_filter_degrades_safely(
    fake_crypto_tvscreener_module, monkeypatch
):
    """Test that screening degrades safely when cooldown lookup fails."""
    tv_service = AsyncMock()
    tv_service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW"],
            "name": ["BTCKRW"],
            "description": ["Bitcoin TV"],
            "price": [150_000_000.0],
            "change_percent": [-0.01],
            "relative_strength_index_14": [45.5],
            "average_directional_index_14": [25.3],
            "volume_24h_in_usd": [156_000_000.0],
            "value_traded": [900_000_000_000.0],
            "market_cap": [2_500_000_000_000_000.0],
            "exchange": ["UPBIT"],
        }
    )

    async def mock_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
        return [{"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0}]

    async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
        return set()

    mock_session = MagicMock()
    mock_session.close = AsyncMock()

    class FailingCooldownService:
        async def is_in_cooldown(self, symbol: str) -> bool:
            raise Exception("Redis connection failed")

        async def record_stop_loss(self, symbol: str) -> None:
            pass

    monkeypatch.setattr(
        screening_crypto,
        "_import_tvscreener",
        lambda: fake_crypto_tvscreener_module,
    )
    monkeypatch.setattr(
        screening_crypto,
        "TvScreenerService",
        lambda timeout=30.0: tv_service,
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_tickers",
        mock_fetch_multiple_tickers,
    )
    monkeypatch.setattr(
        screening_crypto,
        "get_upbit_warning_markets",
        mock_warning_markets,
        raising=False,
    )
    monkeypatch.setattr(
        screening_crypto,
        "AsyncSessionLocal",
        lambda: mock_session,
    )
    monkeypatch.setattr(
        screening_crypto,
        "_get_crypto_trade_cooldown_service",
        lambda: FailingCooldownService(),
    )

    tools = build_tools()
    # Should not raise even when cooldown lookup fails
    result = await tools["screen_stocks"](
        market="crypto",
        limit=5,
    )

    # Results should still be returned
    assert len(result["results"]) >= 1
    assert "KRW-BTC" in [item["symbol"] for item in result["results"]]


@pytest.mark.asyncio
async def test_screen_crypto_includes_voting_signals(
    fake_crypto_tvscreener_module, monkeypatch
):
    """Screen results should include bull_votes, bear_votes, buy_signal."""
    tv_service = AsyncMock()
    tv_service.query_crypto_screener.return_value = pd.DataFrame(
        {
            "symbol": ["UPBIT:BTCKRW"],
            "name": ["BTCKRW"],
            "description": ["Bitcoin TV"],
            "price": [150_000_000.0],
            "change_percent": [-0.01],
            "relative_strength_index_14": [45.5],
            "average_directional_index_14": [25.3],
            "volume_24h_in_usd": [156_000_000.0],
            "value_traded": [900_000_000_000.0],
            "market_cap": [2_500_000_000_000_000.0],
            "exchange": ["UPBIT"],
        }
    )

    async def mock_fetch_multiple_tickers(
        market_codes: list[str],
    ) -> list[dict[str, object]]:
        return [
            {"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0},
        ]

    async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
        return set()

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
        import numpy as np

        assert market_type == "crypto"
        assert count == 50
        # Create data that will produce some voting signals
        close = list(np.linspace(100, 200, 50))
        volume = [1_000.0] * 30 + [5_000.0] * 20  # volume spike at end
        return pd.DataFrame(
            {
                "open": close,
                "high": [c + 10.0 for c in close],
                "low": [c - 10.0 for c in close],
                "close": close,
                "volume": volume,
            }
        )

    monkeypatch.setattr(
        screening_crypto,
        "_import_tvscreener",
        lambda: fake_crypto_tvscreener_module,
    )
    monkeypatch.setattr(
        screening_crypto,
        "TvScreenerService",
        lambda timeout=30.0: tv_service,
    )
    monkeypatch.setattr(
        upbit_service,
        "fetch_multiple_tickers",
        mock_fetch_multiple_tickers,
    )
    monkeypatch.setattr(
        screening_crypto,
        "get_upbit_warning_markets",
        mock_warning_markets,
    )
    monkeypatch.setattr(
        screening_crypto._CRYPTO_MARKET_CAP_CACHE,
        "get",
        mock_market_cap_cache_get,
    )
    monkeypatch.setattr(
        screening_crypto,
        "_fetch_ohlcv_for_indicators",
        mock_fetch_ohlcv,
    )
    monkeypatch.setattr(
        screening_crypto,
        "get_upbit_market_display_names",
        AsyncMock(
            return_value={
                "KRW-BTC": {
                    "korean_name": "비트코인",
                    "english_name": "Bitcoin",
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

    items = result.get("results", [])
    assert len(items) > 0
    for item in items:
        # voting 시그널이 None이면 안 됨 (silent failure 방지)
        assert item.get("bull_votes") is not None, f"{item['symbol']}: bull_votes is None"
        assert item.get("bear_votes") is not None, f"{item['symbol']}: bear_votes is None"
        assert item.get("buy_signal") is not None, f"{item['symbol']}: buy_signal is None"
        assert item.get("sell_signal") is not None, f"{item['symbol']}: sell_signal is None"
        assert "bull_flags" in item
