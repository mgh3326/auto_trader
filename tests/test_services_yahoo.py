from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


class TestYahooService:
    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.download")
    async def test_fetch_ohlcv(self, mock_download, monkeypatch):
        tracing_session = object()
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: tracing_session,
        )
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.settings.yahoo_ohlcv_cache_enabled",
            False,
            raising=False,
        )

        mock_df = pd.DataFrame(
            {
                "open": [100, 101, 102],
                "high": [105, 106, 107],
                "low": [95, 96, 97],
                "close": [103, 104, 105],
                "volume": [1000, 1100, 1200],
            }
        )
        mock_download.return_value = mock_df

        from app.services.brokers.yahoo.client import fetch_ohlcv

        result = await fetch_ohlcv("AAPL", days=3)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        assert "date" in result.columns
        assert "open" in result.columns
        assert "close" in result.columns
        assert mock_download.call_args.kwargs["session"] is tracing_session

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.download")
    async def test_fetch_ohlcv_period_1h_uses_60m_interval(
        self, mock_download, monkeypatch
    ):
        tracing_session = object()
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: tracing_session,
        )
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.settings.yahoo_ohlcv_cache_enabled",
            False,
            raising=False,
        )
        mock_download.return_value = pd.DataFrame(
            {
                "open": [100, 101],
                "high": [105, 106],
                "low": [95, 96],
                "close": [103, 104],
                "volume": [1000, 1100],
            }
        )

        from app.services.brokers.yahoo.client import fetch_ohlcv

        result = await fetch_ohlcv("AAPL", days=2, period="1h")

        assert len(result) == 2
        assert mock_download.call_args.kwargs["interval"] == "60m"

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.Ticker")
    async def test_fetch_price(self, mock_ticker_class, monkeypatch):
        tracing_session = object()
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: tracing_session,
        )

        mock_ticker = MagicMock()
        mock_ticker.fast_info.open = 150.0
        mock_ticker.fast_info.day_high = 155.0
        mock_ticker.fast_info.day_low = 145.0
        mock_ticker.fast_info.last_price = 152.0
        mock_ticker.fast_info.last_volume = 1000000
        mock_ticker_class.return_value = mock_ticker

        from app.services.brokers.yahoo.client import fetch_price

        result = await fetch_price("AAPL")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert "date" in result.columns
        assert "close" in result.columns
        assert mock_ticker_class.call_args.kwargs["session"] is tracing_session

    @pytest.mark.asyncio
    async def test_fetch_price_offloads_blocking_call_to_thread(self, monkeypatch):
        import app.services.brokers.yahoo.client as yahoo

        expected = pd.DataFrame([{"close": 123.45}]).set_index(
            pd.Index(["AAPL"], name="code")
        )

        async def fake_to_thread(func, *args, **kwargs):
            assert func is yahoo._fetch_price_sync
            assert args == ("AAPL",)
            assert kwargs == {}
            return expected

        monkeypatch.setattr(yahoo.asyncio, "to_thread", fake_to_thread)

        result = await yahoo.fetch_price("AAPL")

        assert result is expected

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.Ticker")
    async def test_fetch_fundamental_info(self, mock_ticker_class, monkeypatch):
        tracing_session = object()
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: tracing_session,
        )

        mock_ticker = MagicMock()
        mock_ticker.info = {
            "trailingPE": 12.3,
            "priceToBook": 1.8,
            "trailingEps": 5.6,
            "bookValue": 20.1,
            "trailingAnnualDividendYield": 0.012,
        }
        mock_ticker_class.return_value = mock_ticker

        from app.services.brokers.yahoo.client import fetch_fundamental_info

        result = await fetch_fundamental_info("AAPL")

        assert result == {
            "PER": 12.3,
            "PBR": 1.8,
            "EPS": 5.6,
            "BPS": 20.1,
            "Dividend Yield": 0.012,
        }
        assert mock_ticker_class.call_args.kwargs["session"] is tracing_session
