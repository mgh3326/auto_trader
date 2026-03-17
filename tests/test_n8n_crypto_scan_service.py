"""Unit tests for n8n crypto scan service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def mock_top_coins() -> list[dict]:
    """Top traded coins fixture — 3 coins sorted by trade amount."""
    return [
        {"market": "KRW-BTC", "acc_trade_price_24h": 150_000_000_000},
        {"market": "KRW-ETH", "acc_trade_price_24h": 80_000_000_000},
        {"market": "KRW-XRP", "acc_trade_price_24h": 45_000_000_000},
    ]


@pytest.fixture
def mock_my_coins() -> list[dict]:
    """Holdings fixture — user holds BTC and SOL (SOL not in top 3)."""
    return [
        {"currency": "BTC", "balance": "0.5", "locked": "0"},
        {"currency": "SOL", "balance": "10", "locked": "0"},
    ]


@pytest.fixture
def mock_ohlcv_df() -> pd.DataFrame:
    """OHLCV dataframe with enough rows for RSI/SMA calculation."""
    np.random.seed(42)
    n = 50
    close = pd.Series(np.cumsum(np.random.randn(n)) + 100)
    return pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": [1000] * n,
        }
    )


@pytest.fixture
def mock_tickers() -> list[dict]:
    """Ticker data for BTC, ETH, XRP, SOL."""
    return [
        {
            "market": "KRW-BTC",
            "trade_price": 110_000_000,
            "signed_change_rate": -0.0018,
            "acc_trade_price_24h": 150_000_000_000,
        },
        {
            "market": "KRW-ETH",
            "trade_price": 2_900_000,
            "signed_change_rate": 0.02,
            "acc_trade_price_24h": 80_000_000_000,
        },
        {
            "market": "KRW-XRP",
            "trade_price": 800,
            "signed_change_rate": -0.05,
            "acc_trade_price_24h": 45_000_000_000,
        },
        {
            "market": "KRW-SOL",
            "trade_price": 180_000,
            "signed_change_rate": 0.03,
            "acc_trade_price_24h": 20_000_000_000,
        },
    ]


def _patch_all():
    """Return a dict of patches for all external dependencies."""
    return {
        "top_coins": patch(
            "app.services.n8n_crypto_scan_service.fetch_top_traded_coins",
            new_callable=AsyncMock,
        ),
        "my_coins": patch(
            "app.services.n8n_crypto_scan_service.fetch_my_coins",
            new_callable=AsyncMock,
        ),
        "ohlcv": patch(
            "app.services.n8n_crypto_scan_service.fetch_ohlcv",
            new_callable=AsyncMock,
        ),
        "tickers": patch(
            "app.services.n8n_crypto_scan_service.fetch_multiple_tickers",
            new_callable=AsyncMock,
        ),
        "fear_greed": patch(
            "app.services.n8n_crypto_scan_service.fetch_fear_greed",
            new_callable=AsyncMock,
        ),
        "korean_name": patch(
            "app.services.n8n_crypto_scan_service.get_upbit_korean_name_by_coin",
            new_callable=AsyncMock,
        ),
    }


@pytest.mark.unit
class TestFetchCryptoScan:
    """Tests for fetch_crypto_scan service function."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_all_fields(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Scan returns coins with indicators, BTC context, F&G, and summary."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers
            m_fg.return_value = {
                "value": 34,
                "label": "Fear",
                "previous": 28,
                "trend": "improving",
            }
            m_name.return_value = "테스트코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, ohlcv_days=50)

        assert result["success"] is True
        assert "btc_context" in result
        assert "fear_greed" in result
        assert "coins" in result
        assert "summary" in result
        assert "errors" in result
        # Should have 4 coins: BTC, ETH, XRP (top 3) + SOL (holding)
        assert len(result["coins"]) == 4
        # Summary should reflect correct counts
        assert result["summary"]["top_n_count"] == 3
        assert result["summary"]["holdings_added"] == 1
        assert result["summary"]["total_scanned"] == 4

    @pytest.mark.asyncio
    async def test_coins_sorted_by_rsi_ascending(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Coins should be sorted by RSI ascending (most oversold first)."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = []  # no holdings
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3)

        coins = result["coins"]
        rsi_values = [
            c["indicators"]["rsi14"]
            for c in coins
            if c["indicators"]["rsi14"] is not None
        ]
        assert rsi_values == sorted(rsi_values), "Coins must be sorted by RSI ascending"

    @pytest.mark.asyncio
    async def test_holdings_added_outside_top_n(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """Holdings not in top_n should still appear in coins."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins  # holds BTC + SOL
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_holdings=True)

        symbols = [c["symbol"] for c in result["coins"]]
        assert "KRW-SOL" in symbols, "SOL (holding but not top 3) should be included"
        sol_coin = next(c for c in result["coins"] if c["symbol"] == "KRW-SOL")
        assert sol_coin["is_holding"] is True
        assert sol_coin["rank"] is None  # not in top N

    @pytest.mark.asyncio
    async def test_include_holdings_false_excludes_extra(
        self,
        mock_top_coins: list[dict],
        mock_my_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """When include_holdings=False, only top_n coins appear."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = mock_my_coins
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_holdings=False)

        symbols = [c["symbol"] for c in result["coins"]]
        assert "KRW-SOL" not in symbols
        assert len(result["coins"]) == 3

    @pytest.mark.asyncio
    async def test_include_fear_greed_false_returns_none(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
        mock_tickers: list[dict],
    ) -> None:
        """When include_fear_greed=False, fear_greed field is None."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins[:1]
            m_my.return_value = []
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = mock_tickers[:1]
            m_fg.return_value = {
                "value": 34,
                "label": "Fear",
                "previous": 28,
                "trend": "improving",
            }
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1, include_fear_greed=False)

        assert result["fear_greed"] is None
        m_fg.assert_not_called()

    @pytest.mark.asyncio
    async def test_ohlcv_failure_produces_null_indicators(
        self,
        mock_top_coins: list[dict],
        mock_tickers: list[dict],
    ) -> None:
        """When OHLCV fetch fails for a coin, indicators should be null."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins[:1]
            m_my.return_value = []
            m_ohlcv.side_effect = Exception("Upbit API error")
            m_tickers.return_value = mock_tickers[:1]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1)

        assert result["success"] is True  # partial failure is not fatal
        assert len(result["coins"]) == 1
        coin = result["coins"][0]
        assert coin["indicators"]["rsi14"] is None
        assert coin["indicators"]["sma20"] is None
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_crash_threshold_rank_based(
        self,
        mock_top_coins: list[dict],
        mock_ohlcv_df: pd.DataFrame,
    ) -> None:
        """Crash data should use rank-based thresholds from DailyScanner."""
        patches = _patch_all()
        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            # BTC is rank 1 (top 10 threshold = 0.06)
            m_top.return_value = mock_top_coins
            m_my.return_value = []
            m_ohlcv.return_value = mock_ohlcv_df
            m_tickers.return_value = [
                {
                    "market": "KRW-BTC",
                    "trade_price": 110_000_000,
                    "signed_change_rate": -0.07,  # exceeds 0.06 threshold
                    "acc_trade_price_24h": 150_000_000_000,
                },
                {
                    "market": "KRW-ETH",
                    "trade_price": 2_900_000,
                    "signed_change_rate": -0.02,  # below threshold
                    "acc_trade_price_24h": 80_000_000_000,
                },
                {
                    "market": "KRW-XRP",
                    "trade_price": 800,
                    "signed_change_rate": -0.03,
                    "acc_trade_price_24h": 45_000_000_000,
                },
            ]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3, include_crash=True)

        btc = next(c for c in result["coins"] if c["symbol"] == "KRW-BTC")
        assert btc["crash"] is not None
        assert btc["crash"]["triggered"] is True
        assert btc["crash"]["threshold"] == 0.06  # top 10 threshold

    @pytest.mark.asyncio
    async def test_sma_cross_detection(self) -> None:
        """SMA20 golden cross should be detected correctly."""
        patches = _patch_all()
        # Build OHLCV where last candle crosses above SMA20
        # prev_close < prev_sma20 AND curr_close > curr_sma20
        n = 25
        # Price starts below SMA20 then jumps above
        close_values = [100.0] * 20 + [95.0, 94.0, 93.0, 92.0, 105.0]
        close = pd.Series(close_values)
        df = pd.DataFrame(
            {
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": [1000] * n,
            }
        )

        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = [
                {"market": "KRW-TEST", "acc_trade_price_24h": 1_000_000},
            ]
            m_my.return_value = []
            m_ohlcv.return_value = df
            m_tickers.return_value = [
                {
                    "market": "KRW-TEST",
                    "trade_price": 105,
                    "signed_change_rate": 0.14,
                    "acc_trade_price_24h": 1_000_000,
                },
            ]
            m_fg.return_value = None
            m_name.return_value = "테스트"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=1, include_sma_cross=True)

        coin = result["coins"][0]
        assert coin["sma_cross"] is not None
        assert coin["sma_cross"]["type"] == "golden"

    @pytest.mark.asyncio
    async def test_rsi_null_sorted_last(
        self,
        mock_top_coins: list[dict],
        mock_tickers: list[dict],
    ) -> None:
        """Coins with null RSI should appear at end of sorted list."""
        patches = _patch_all()
        # Use realistic price data with both ups and downs to get valid RSI
        np.random.seed(42)
        close_values = pd.Series(np.cumsum(np.random.randn(50)) + 100)
        ohlcv_ok = pd.DataFrame(
            {
                "close": close_values,
                "open": close_values - 1,
                "high": close_values + 2,
                "low": close_values - 2,
                "volume": [1000] * 50,
            }
        )
        ohlcv_empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        async def alternating_ohlcv(*args, **kwargs):
            # BTC gets empty (null RSI), ETH and XRP get real data
            market = args[0] if args else kwargs.get("market", "")
            if market == "KRW-BTC":
                return ohlcv_empty
            return ohlcv_ok

        with (
            patches["top_coins"] as m_top,
            patches["my_coins"] as m_my,
            patches["ohlcv"] as m_ohlcv,
            patches["tickers"] as m_tickers,
            patches["fear_greed"] as m_fg,
            patches["korean_name"] as m_name,
        ):
            m_top.return_value = mock_top_coins
            m_my.return_value = []
            m_ohlcv.side_effect = alternating_ohlcv
            m_tickers.return_value = mock_tickers[:3]
            m_fg.return_value = None
            m_name.return_value = "코인"

            from app.services.n8n_crypto_scan_service import fetch_crypto_scan

            result = await fetch_crypto_scan(top_n=3)

        coins = result["coins"]
        # BTC (null RSI) should be last
        last_coin = coins[-1]
        assert last_coin["symbol"] == "KRW-BTC"
        assert last_coin["indicators"]["rsi14"] is None
