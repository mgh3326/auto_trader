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
    @patch("app.services.brokers.yahoo.client.yf.Ticker")
    async def test_fetch_fast_info_degrades_nonetype_flake_to_none(
        self, mock_ticker_class, monkeypatch
    ):
        """A yfinance internal "'NoneType' object is not subscriptable" raised on lazy
        fast_info attribute access must degrade that field to None, not crash the whole
        quote (ROB-365 bug 2). .get() still works, so it is the attribute path that flakes.
        """
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: object(),
        )

        class _FlakyFastInfo:
            def __getattr__(self, name):
                raise TypeError("'NoneType' object is not subscriptable")

            def get(self, key, default=None):
                return default

        mock_ticker = MagicMock()
        mock_ticker.fast_info = _FlakyFastInfo()
        mock_ticker_class.return_value = mock_ticker

        from app.services.brokers.yahoo.client import fetch_fast_info

        result = await fetch_fast_info("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["close"] is None
        assert result["previous_close"] is None

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.Ticker")
    async def test_fetch_fast_info_still_retries_and_raises_on_crumb_error(
        self, mock_ticker_class, monkeypatch
    ):
        """Crumb/auth errors must still bubble (and trigger the fresh-session retry),
        not be swallowed by the NoneType-flake hardening (ROB-365 bug 2 regression guard)."""
        sessions: list[object] = []

        def _build():
            session = object()
            sessions.append(session)
            return session

        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            _build,
        )

        class _CrumbFastInfo:
            def __getattr__(self, name):
                raise RuntimeError("invalid crumb")

            def get(self, key, default=None):
                raise RuntimeError("invalid crumb")

        mock_ticker = MagicMock()
        mock_ticker.fast_info = _CrumbFastInfo()
        mock_ticker_class.return_value = mock_ticker

        from app.services.brokers.yahoo.client import fetch_fast_info

        with pytest.raises(RuntimeError, match="invalid crumb"):
            await fetch_fast_info("AAPL")

        assert len(sessions) == 2  # original attempt + one fresh-session crumb retry

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
            # ROB-440: ROE (percent) now extracted; None here (mock .info omits
            # returnOnEquity) — fail-closed.
            "ROE": None,
            # ROB-440 Part 2: 52w high/low (price); None here (mock omits them).
            "yearHigh": None,
            "yearLow": None,
            # ROB-440: marketCap; None here (mock omits it).
            "marketCap": None,
        }
        assert mock_ticker_class.call_args.kwargs["session"] is tracing_session
