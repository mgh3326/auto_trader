from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service_module
from app.services.brokers.upbit.client import fetch_ohlcv, fetch_price


class TestUpbitService:
    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_ohlcv(self, mock_request, monkeypatch):
        monkeypatch.setattr(
            upbit_service_module.settings,
            "upbit_ohlcv_cache_enabled",
            False,
            raising=False,
        )

        mock_data = [
            {
                "candle_date_time_kst": "2023-12-01T00:00:00",
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "candle_acc_trade_volume": 100.0,
                "candle_acc_trade_price": 4550000000.0,
            }
        ]
        mock_request.return_value = mock_data

        result = await fetch_ohlcv("KRW-BTC", days=1)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert "date" in result.columns
        assert "open" in result.columns
        assert "high" in result.columns
        assert "low" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "value" in result.columns

    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_ohlcv_4h_uses_minutes_240(self, mock_request):
        mock_request.return_value = []

        await upbit_service_module.fetch_ohlcv("KRW-BTC", days=300, period="4h")

        called_url = mock_request.await_args.args[0]
        called_params = mock_request.await_args.args[1]
        assert called_url.endswith("/candles/minutes/240")
        assert called_params["count"] == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("period", "expected_endpoint"),
        [
            ("1m", "/candles/minutes/1"),
            ("5m", "/candles/minutes/5"),
            ("15m", "/candles/minutes/15"),
            ("30m", "/candles/minutes/30"),
        ],
    )
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_ohlcv_minute_periods_use_expected_endpoints(
        self, mock_request, period, expected_endpoint
    ):
        mock_request.return_value = []

        await upbit_service_module.fetch_ohlcv("KRW-BTC", days=300, period=period)

        called_url = mock_request.await_args.args[0]
        called_params = mock_request.await_args.args[1]
        assert called_url.endswith(expected_endpoint)
        assert called_params["count"] == 200

    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_price(self, mock_request):
        mock_data = [
            {
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "acc_trade_volume_24h": 100.0,
                "acc_trade_price_24h": 4550000000.0,
            }
        ]
        mock_request.return_value = mock_data

        result = await fetch_price("KRW-BTC")

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        assert "date" in result.columns
        assert "time" in result.columns
        assert "open" in result.columns
        assert "high" in result.columns
        assert "low" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        assert "value" in result.columns

    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_ohlcv_raw_keeps_current_upbit_mapping(self, mock_request):
        mock_request.return_value = [
            {
                "candle_date_time_kst": "2023-12-01T00:00:00",
                "opening_price": 45000000,
                "high_price": 46000000,
                "low_price": 44000000,
                "trade_price": 45500000,
                "candle_acc_trade_volume": 100.0,
                "candle_acc_trade_price": 4550000000.0,
            }
        ]

        df = await upbit_service_module._fetch_ohlcv_raw(
            "KRW-BTC", days=2, period="day", end_date=None
        )

        assert list(df.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "value",
        ]

    @pytest.mark.asyncio
    async def test_fetch_krw_cash_summary_includes_locked(self, monkeypatch):
        monkeypatch.setattr(
            upbit_service_module,
            "fetch_my_coins",
            AsyncMock(
                return_value=[
                    {"currency": "KRW", "balance": "500000.0", "locked": "200000.0"}
                ]
            ),
        )

        summary = await upbit_service_module.fetch_krw_cash_summary()

        assert summary["balance"] == pytest.approx(700000.0)
        assert summary["orderable"] == pytest.approx(500000.0)
        assert summary["balance"] == summary["orderable"] + 200000.0

    @pytest.mark.asyncio
    async def test_fetch_krw_orderable_balance_reads_summary(self, monkeypatch):
        mock_summary = AsyncMock(
            return_value={"balance": 700000.0, "orderable": 500000.0}
        )
        monkeypatch.setattr(
            upbit_service_module,
            "fetch_krw_cash_summary",
            mock_summary,
        )

        result = await upbit_service_module.fetch_krw_orderable_balance()

        assert result == pytest.approx(500000.0)
        mock_summary.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_krw_cash_summary_returns_zero_without_krw(self, monkeypatch):
        monkeypatch.setattr(
            upbit_service_module,
            "fetch_my_coins",
            AsyncMock(
                return_value=[{"currency": "BTC", "balance": "0.1", "locked": "0"}]
            ),
        )

        summary = await upbit_service_module.fetch_krw_cash_summary()

        assert summary == {"balance": 0.0, "orderable": 0.0}
