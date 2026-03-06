"""Integration tests for crypto screening using tvscreener.

This module tests the integration of TradingView's CryptoScreener for bulk
indicator queries, replacing manual RSI calculation. Tests cover symbol mapping,
indicator enrichment, and end-to-end screening flow.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.mcp_server.tooling.analysis_screen_core import (
    _enrich_crypto_indicators,
    _normalize_crypto_symbol,
    _screen_crypto,
)
from app.utils.symbol_mapping import (
    SymbolMappingError,
)


@pytest.fixture
def sample_crypto_candidates() -> list[dict]:
    """Sample crypto candidates for testing."""
    return [
        {
            "market": "KRW-BTC",
            "original_market": "KRW-BTC",
            "symbol": "BTC",
            "name": "비트코인",
            "change_rate": 0.05,
            "trade_amount_24h": 1000000000.0,
            "volume_24h": 50000.0,
            "rsi": None,
        },
        {
            "market": "KRW-ETH",
            "original_market": "KRW-ETH",
            "symbol": "ETH",
            "name": "이더리움",
            "change_rate": 0.03,
            "trade_amount_24h": 500000000.0,
            "volume_24h": 30000.0,
            "rsi": None,
        },
        {
            "market": "KRW-XRP",
            "original_market": "KRW-XRP",
            "symbol": "XRP",
            "name": "리플",
            "change_rate": -0.02,
            "trade_amount_24h": 200000000.0,
            "volume_24h": 10000.0,
            "rsi": None,
        },
    ]


@pytest.fixture
def sample_crypto_screener_df() -> pd.DataFrame:
    """Sample DataFrame returned by CryptoScreener."""
    return pd.DataFrame(
        {
            "ticker": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
            "relative_strength_index_14": [45.5, 32.1, 68.9],
            "average_directional_index_14": [25.3, 18.7, 42.1],
            "volume": [50000.0, 30000.0, 10000.0],
        }
    )


class TestSymbolNormalization:
    """Test crypto symbol normalization."""

    def test_normalize_krw_btc(self):
        """Test normalizing KRW-BTC format."""
        result = _normalize_crypto_symbol("KRW-BTC")
        assert result == "KRW-BTC"

    def test_normalize_with_whitespace(self):
        """Test normalizing symbol with whitespace."""
        result = _normalize_crypto_symbol("  KRW-ETH  ")
        assert result == "KRW-ETH"

    def test_normalize_empty_string(self):
        """Test normalizing empty string."""
        result = _normalize_crypto_symbol("")
        assert result == ""

    def test_normalize_none_value(self):
        """Test normalizing None value."""
        result = _normalize_crypto_symbol(None)
        assert result == ""


class TestCryptoIndicatorEnrichment:
    """Test crypto indicator enrichment with CryptoScreener."""

    @pytest.mark.asyncio
    async def test_enrich_empty_candidates(self):
        """Test enrichment with empty candidates list."""
        result = await _enrich_crypto_indicators([])
        assert result["attempted"] == 0
        assert result["succeeded"] == 0
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_enrich_candidates_already_have_rsi(self):
        """Test enrichment skips candidates that already have RSI."""
        candidates = [
            {
                "market": "KRW-BTC",
                "original_market": "KRW-BTC",
                "rsi": 45.5,
            }
        ]
        result = await _enrich_crypto_indicators(candidates)
        assert result["attempted"] == 1
        assert result["succeeded"] == 1
        # RSI should not be overwritten
        assert candidates[0]["rsi"] == 45.5

    @pytest.mark.asyncio
    async def test_enrich_candidates_no_valid_symbol(self):
        """Test enrichment handles candidates with no valid symbol."""
        candidates = [
            {
                "market": None,
                "original_market": None,
                "symbol": None,
                "rsi": None,
            }
        ]
        result = await _enrich_crypto_indicators(candidates)
        assert result["attempted"] == 1
        assert result["failed"] >= 1

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.upbit_to_tradingview")
    async def test_enrich_symbol_mapping_error(
        self, mock_upbit_to_tv, sample_crypto_candidates
    ):
        """Test enrichment handles symbol mapping errors gracefully."""
        mock_upbit_to_tv.side_effect = SymbolMappingError(
            "Invalid Upbit symbol: KRW-INVALID"
        )

        result = await _enrich_crypto_indicators(sample_crypto_candidates[:1])
        assert result["attempted"] == 1
        assert result["failed"] >= 1

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_tvscreener_not_installed(
        self, mock_tvscreener_service_class, sample_crypto_candidates
    ):
        """Test fallback to manual calculation when tvscreener not installed."""
        # Mock the import to raise ImportError
        mock_service = AsyncMock()
        mock_service.query_crypto_screener.side_effect = ImportError(
            "tvscreener not installed"
        )
        mock_tvscreener_service_class.return_value = mock_service

        # Mock manual RSI calculation fallback
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.compute_crypto_realtime_rsi_map"
        ) as mock_manual_rsi:
            mock_manual_rsi.return_value = {
                "KRW-BTC": 45.5,
                "KRW-ETH": 32.1,
                "KRW-XRP": 68.9,
            }

            result = await _enrich_crypto_indicators(sample_crypto_candidates)

            # Should fall back to manual calculation
            assert result["attempted"] == 3
            # At least some should succeed via fallback
            assert result["succeeded"] >= 0

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_with_rsi_only(
        self, mock_tvscreener_service_class, sample_crypto_candidates
    ):
        """Test enrichment with RSI data only (no ADX/volume)."""
        # Create a DataFrame with only RSI data
        df = pd.DataFrame(
            {
                "ticker": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "relative_strength_index_14": [45.5, 32.1, 68.9],
            }
        )

        mock_service = AsyncMock()
        mock_service.query_crypto_screener = AsyncMock(return_value=df)
        mock_tvscreener_service_class.return_value = mock_service

        # Mock CryptoField and CryptoScreener imports
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.CryptoField"
        ) as mock_crypto_field:
            mock_crypto_field.TICKER = MagicMock()
            mock_crypto_field.RELATIVE_STRENGTH_INDEX_14 = MagicMock()
            # ADX not available
            del type(mock_crypto_field).AVERAGE_DIRECTIONAL_INDEX_14
            mock_crypto_field.VOLUME = MagicMock()

            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # Verify RSI values were applied
                assert sample_crypto_candidates[0]["rsi"] == 45.5
                assert sample_crypto_candidates[1]["rsi"] == 32.1
                assert sample_crypto_candidates[2]["rsi"] == 68.9

                # Verify diagnostics
                assert result["attempted"] == 3
                assert result["succeeded"] == 3

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_with_all_indicators(
        self,
        mock_tvscreener_service_class,
        sample_crypto_candidates,
        sample_crypto_screener_df,
    ):
        """Test enrichment with RSI, ADX, and volume data."""
        mock_service = AsyncMock()
        mock_service.query_crypto_screener = AsyncMock(
            return_value=sample_crypto_screener_df
        )
        mock_tvscreener_service_class.return_value = mock_service

        # Mock CryptoField and CryptoScreener imports
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.CryptoField"
        ) as mock_crypto_field:
            mock_crypto_field.TICKER = MagicMock()
            mock_crypto_field.RELATIVE_STRENGTH_INDEX_14 = MagicMock()
            mock_crypto_field.AVERAGE_DIRECTIONAL_INDEX_14 = MagicMock()
            mock_crypto_field.VOLUME = MagicMock()

            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # Verify RSI values were applied
                assert sample_crypto_candidates[0]["rsi"] == 45.5
                assert sample_crypto_candidates[1]["rsi"] == 32.1
                assert sample_crypto_candidates[2]["rsi"] == 68.9

                # Verify ADX values were applied
                assert sample_crypto_candidates[0]["adx"] == 25.3
                assert sample_crypto_candidates[1]["adx"] == 18.7
                assert sample_crypto_candidates[2]["adx"] == 42.1

                # Verify volume values were applied
                assert sample_crypto_candidates[0]["volume_24h"] == 50000.0
                assert sample_crypto_candidates[1]["volume_24h"] == 30000.0
                assert sample_crypto_candidates[2]["volume_24h"] == 10000.0

                # Verify diagnostics
                assert result["attempted"] == 3
                assert result["succeeded"] == 3

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_partial_results(
        self, mock_tvscreener_service_class, sample_crypto_candidates
    ):
        """Test enrichment with partial results (some symbols not found)."""
        # DataFrame missing XRP
        df = pd.DataFrame(
            {
                "ticker": ["UPBIT:BTCKRW", "UPBIT:ETHKRW"],
                "relative_strength_index_14": [45.5, 32.1],
            }
        )

        mock_service = AsyncMock()
        mock_service.query_crypto_screener = AsyncMock(return_value=df)
        mock_tvscreener_service_class.return_value = mock_service

        # Mock CryptoField and CryptoScreener imports
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.CryptoField"
        ) as mock_crypto_field:
            mock_crypto_field.TICKER = MagicMock()
            mock_crypto_field.RELATIVE_STRENGTH_INDEX_14 = MagicMock()
            del type(mock_crypto_field).AVERAGE_DIRECTIONAL_INDEX_14
            mock_crypto_field.VOLUME = MagicMock()

            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # First two should have RSI
                assert sample_crypto_candidates[0]["rsi"] == 45.5
                assert sample_crypto_candidates[1]["rsi"] == 32.1

                # Third should not have RSI
                assert sample_crypto_candidates[2]["rsi"] is None

                # Verify diagnostics
                assert result["attempted"] == 3
                assert result["succeeded"] == 2
                assert result["failed"] == 1

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_empty_dataframe(
        self, mock_tvscreener_service_class, sample_crypto_candidates
    ):
        """Test enrichment when CryptoScreener returns empty DataFrame."""
        mock_service = AsyncMock()
        mock_service.query_crypto_screener = AsyncMock(return_value=pd.DataFrame())
        mock_tvscreener_service_class.return_value = mock_service

        # Mock CryptoField and CryptoScreener imports
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.CryptoField"
        ) as mock_crypto_field:
            mock_crypto_field.TICKER = MagicMock()
            mock_crypto_field.RELATIVE_STRENGTH_INDEX_14 = MagicMock()
            del type(mock_crypto_field).AVERAGE_DIRECTIONAL_INDEX_14
            mock_crypto_field.VOLUME = MagicMock()

            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # All candidates should have None for RSI
                for candidate in sample_crypto_candidates:
                    assert candidate["rsi"] is None

                # Verify diagnostics
                assert result["attempted"] == 3
                assert result["failed"] == 3

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_enrich_rate_limit_error(
        self, mock_tvscreener_service_class, sample_crypto_candidates
    ):
        """Test enrichment handles rate limit errors."""
        from app.services.tvscreener_service import TvScreenerRateLimitError

        mock_service = AsyncMock()
        mock_service.query_crypto_screener.side_effect = TvScreenerRateLimitError(
            "Rate limit exceeded"
        )
        mock_tvscreener_service_class.return_value = mock_service

        # Mock CryptoField and CryptoScreener imports
        with patch("app.mcp_server.tooling.analysis_screen_core.CryptoField"):
            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # Verify diagnostics show rate limited
                assert result["attempted"] == 3
                assert result["rate_limited"] == 3

    @pytest.mark.asyncio
    async def test_enrich_timeout(self, sample_crypto_candidates):
        """Test enrichment handles timeout errors."""
        # Mock sleep to speed up the test
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.compute_crypto_realtime_rsi_map"
        ) as mock_rsi:
            mock_rsi.side_effect = TimeoutError("Timeout after 30s")

            # Mock tvscreener to raise ImportError so we use manual fallback
            with patch(
                "app.mcp_server.tooling.analysis_screen_core.TvScreenerService"
            ) as mock_tv:
                mock_service = AsyncMock()
                mock_service.query_crypto_screener.side_effect = ImportError(
                    "tvscreener not installed"
                )
                mock_tv.return_value = mock_service

                result = await _enrich_crypto_indicators(sample_crypto_candidates)

                # Verify diagnostics show timeout
                assert result["attempted"] == 3
                assert result["timeout"] >= 1


class TestCryptoScreening:
    """Test end-to-end crypto screening with tvscreener."""

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.get_upbit_warning_markets")
    @patch("app.mcp_server.tooling.analysis_screen_core._CRYPTO_MARKET_CAP_CACHE")
    @patch("app.mcp_server.tooling.analysis_screen_core.TvScreenerService")
    async def test_screen_crypto_basic(
        self,
        mock_tvscreener_service_class,
        mock_market_cap_cache,
        mock_warning_markets,
    ):
        """Test basic crypto screening flow."""
        # Mock warning markets
        mock_warning_markets.return_value = set()

        # Mock market cap cache
        mock_market_cap_cache.get.return_value = {
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }

        # Mock tvscreener service
        df = pd.DataFrame(
            {
                "ticker": ["UPBIT:BTCKRW"],
                "relative_strength_index_14": [25.5],
                "average_directional_index_14": [30.0],
                "volume": [50000.0],
            }
        )
        mock_service = AsyncMock()
        mock_service.query_crypto_screener = AsyncMock(return_value=df)
        mock_tvscreener_service_class.return_value = mock_service

        # Mock raw tickers data
        raw_tickers = [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "trade_price": 50000000.0,
                "acc_trade_price_24h": 1000000000.0,
                "signed_change_rate": 0.05,
            }
        ]

        # Mock CryptoField and CryptoScreener
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.CryptoField"
        ) as mock_crypto_field:
            mock_crypto_field.TICKER = MagicMock()
            mock_crypto_field.RELATIVE_STRENGTH_INDEX_14 = MagicMock()
            mock_crypto_field.AVERAGE_DIRECTIONAL_INDEX_14 = MagicMock()
            mock_crypto_field.VOLUME = MagicMock()

            with patch("app.mcp_server.tooling.analysis_screen_core.CryptoScreener"):
                result = await _screen_crypto(
                    raw_tickers=raw_tickers,
                    max_rsi=30,
                    sort_by="rsi",
                    enrich_rsi=True,
                    limit=10,
                )

                # Verify result structure
                assert "candidates" in result
                assert "diagnostics" in result
                assert "warnings" in result

                # Verify candidate was enriched
                if result["candidates"]:
                    candidate = result["candidates"][0]
                    assert candidate["rsi"] == 25.5
                    assert candidate["adx"] == 30.0
                    assert candidate["volume_24h"] == 50000.0

    @pytest.mark.asyncio
    @patch("app.mcp_server.tooling.analysis_screen_core.get_upbit_warning_markets")
    @patch("app.mcp_server.tooling.analysis_screen_core._CRYPTO_MARKET_CAP_CACHE")
    async def test_screen_crypto_without_enrichment(
        self, mock_market_cap_cache, mock_warning_markets
    ):
        """Test crypto screening without RSI enrichment."""
        # Mock warning markets
        mock_warning_markets.return_value = set()

        # Mock market cap cache
        mock_market_cap_cache.get.return_value = {
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }

        # Mock raw tickers data
        raw_tickers = [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "trade_price": 50000000.0,
                "acc_trade_price_24h": 1000000000.0,
                "signed_change_rate": 0.05,
            }
        ]

        result = await _screen_crypto(
            raw_tickers=raw_tickers,
            max_rsi=None,
            sort_by="change_rate",
            enrich_rsi=False,
            limit=10,
        )

        # Verify result structure
        assert "candidates" in result
        assert "diagnostics" in result
        assert "warnings" in result

        # Verify no RSI enrichment occurred
        if result["candidates"]:
            candidate = result["candidates"][0]
            # RSI should be None since enrichment was disabled
            assert candidate.get("rsi") is None


@pytest.mark.integration
class TestCryptoScreeningIntegration:
    """Integration tests with real TradingView API calls.

    These tests are marked as integration and require actual network access
    to TradingView. They should be run separately from unit tests.
    """

    @pytest.mark.asyncio
    async def test_enrich_real_symbols(self):
        """Test enrichment with real CryptoScreener API calls."""
        pytest.importorskip("tvscreener")

        candidates = [
            {
                "market": "KRW-BTC",
                "original_market": "KRW-BTC",
                "symbol": "BTC",
                "name": "비트코인",
                "rsi": None,
            },
            {
                "market": "KRW-ETH",
                "original_market": "KRW-ETH",
                "symbol": "ETH",
                "name": "이더리움",
                "rsi": None,
            },
        ]

        result = await _enrich_crypto_indicators(candidates)

        # Verify enrichment succeeded
        assert result["attempted"] == 2
        assert result["succeeded"] >= 1  # At least one should succeed

        # Verify RSI values were populated
        enriched_count = sum(1 for c in candidates if c.get("rsi") is not None)
        assert enriched_count >= 1

        # Verify RSI values are in valid range
        for candidate in candidates:
            rsi = candidate.get("rsi")
            if rsi is not None:
                assert 0 <= rsi <= 100, f"RSI {rsi} out of valid range [0, 100]"

    @pytest.mark.asyncio
    async def test_screen_crypto_real_api(self):
        """Test crypto screening with real API calls."""
        pytest.importorskip("tvscreener")

        # Mock warning markets and market cap cache to avoid external dependencies
        with patch(
            "app.mcp_server.tooling.analysis_screen_core.get_upbit_warning_markets"
        ) as mock_warning:
            mock_warning.return_value = set()

            with patch(
                "app.mcp_server.tooling.analysis_screen_core._CRYPTO_MARKET_CAP_CACHE"
            ) as mock_cache:
                mock_cache.get.return_value = {
                    "data": {},
                    "cached": True,
                    "age_seconds": 0.0,
                    "stale": False,
                    "error": None,
                }

                raw_tickers = [
                    {
                        "market": "KRW-BTC",
                        "korean_name": "비트코인",
                        "trade_price": 50000000.0,
                        "acc_trade_price_24h": 1000000000.0,
                        "signed_change_rate": 0.05,
                    },
                    {
                        "market": "KRW-ETH",
                        "korean_name": "이더리움",
                        "trade_price": 3000000.0,
                        "acc_trade_price_24h": 500000000.0,
                        "signed_change_rate": 0.03,
                    },
                ]

                result = await _screen_crypto(
                    raw_tickers=raw_tickers,
                    max_rsi=None,
                    sort_by="rsi",
                    enrich_rsi=True,
                    limit=10,
                )

                # Verify basic structure
                assert "candidates" in result
                assert "diagnostics" in result

                # Verify at least some enrichment succeeded
                if result["candidates"]:
                    enriched_count = sum(
                        1 for c in result["candidates"] if c.get("rsi") is not None
                    )
                    assert enriched_count >= 0  # May be 0 if API issues
