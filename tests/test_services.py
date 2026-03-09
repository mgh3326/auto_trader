"""
Tests for service modules.
"""

import logging
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service_module
from app.services import stock_info_service
from app.services.brokers.upbit.client import fetch_ohlcv, fetch_price


class TestUpbitService:
    """Test Upbit service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.upbit.client._request_json")
    async def test_fetch_ohlcv(self, mock_request, monkeypatch):
        """Test fetching OHLCV data."""
        monkeypatch.setattr(
            upbit_service_module.settings,
            "upbit_ohlcv_cache_enabled",
            False,
            raising=False,
        )

        # Mock response
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
        """Test fetching current price."""
        # Mock response
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
        # 실제 fetch_price 함수에서 반환하는 컬럼들 확인
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

        assert summary["balance"] == 700000.0
        assert summary["orderable"] == 500000.0
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

        assert result == 500000.0
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


class TestKISService:
    """Test KIS service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_kis_client_initialization(self, mock_client_class):
        """Test KIS client initialization."""
        # Mock client
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Import and test the actual class
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        assert client is not None
        assert hasattr(client, "_hdr_base")

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_kis_volume_rank(self, mock_client_class):
        """Test KIS volume rank functionality."""
        # Mock client
        mock_client = AsyncMock()

        # Mock response - r.json()이 동기적으로 동작하도록 설정
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "hts_kor_isnm": "삼성전자",
                    "stck_cntg_hour": "15:30:00",
                    "stck_prpr": "50000",
                }
            ],
        }

        # Mock client의 get 메서드 설정
        mock_client.get.return_value = mock_response

        # Mock client class for both direct calls and context manager
        mock_client_class.return_value = mock_client
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        # Import and test the actual function
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        # Mock the token loading
        with patch.object(client, "_ensure_token"):
            result = await client.volume_rank()

            assert isinstance(result, list)
            assert len(result) > 0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_fetch_my_stocks_inqr_dvsn_domestic(self, mock_client_class):
        """Verify INQR_DVSN is set to '00' for domestic stock queries."""
        # Setup mock client and response
        mock_client = AsyncMock()

        # Mock response for empty holdings (end of pagination)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": [],
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        # Set up mock_client.get() to return the response
        mock_client.get.return_value = mock_response

        # Set up context manager for async with
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        # Import and test the actual function
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        # Mock the token loading
        with patch.object(client, "_ensure_token"):
            # Call fetch_my_stocks for domestic stocks (is_overseas=False)
            await client.fetch_my_stocks(is_mock=False, is_overseas=False)

            # Verify the params passed to HTTP request
            call_args = mock_client.get.call_args
            assert "params" in call_args.kwargs
            params = call_args.kwargs["params"]

            # Verify INQR_DVSN parameter is set to "00" (not "02")
            assert params["INQR_DVSN"] == "00"

            # Verify other key domestic stock parameters are also set correctly
            assert params["AFHR_FLPR_YN"] == "N"
            assert params["UNPR_DVSN"] == "01"
            assert params["PRCS_DVSN"] == "01"

            # Verify tr_id header is set to TTTC8434R for real trading
            call_kwargs = mock_client.get.call_args.kwargs
            assert "headers" in call_kwargs
            headers = call_kwargs["headers"]
            assert headers["tr_id"] == "TTTC8434R"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_success(
        self, mock_client_class, monkeypatch
    ):
        """inquire-balance(output2)에서 국내 현금 잔고를 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "dnca_tot_amt": "1140000",
                    "stck_cash_ord_psbl_amt": "1110000",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_domestic_cash_balance(is_mock=False)

        assert result["dnca_tot_amt"] == 1140000.0
        assert result["stck_cash_ord_psbl_amt"] == 1110000.0
        assert result["raw"]["dnca_tot_amt"] == "1140000"

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        headers = call_args.kwargs["headers"]
        assert params["INQR_DVSN"] == "00"
        assert headers["tr_id"] == "TTTC8434R"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_fallback_ord_psbl_cash(
        self, mock_client_class, monkeypatch
    ):
        """stck_cash_ord_psbl_amt가 없으면 ord_psbl_cash를 fallback으로 사용한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "dnca_tot_amt": "1140000",
                    "ord_psbl_cash": "950000",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_domestic_cash_balance(is_mock=False)

        assert result["dnca_tot_amt"] == 1140000.0
        assert result["stck_cash_ord_psbl_amt"] == 950000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_api_error_raises(
        self, mock_client_class, monkeypatch
    ):
        """API 오류 응답은 RuntimeError로 전달한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW99999",
            "msg1": "failure",
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(RuntimeError, match="EGW99999"):
                await client.inquire_domestic_cash_balance(is_mock=False)

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_overseas_margin_parses_extended_orderable_fields(
        self, mock_client_class, monkeypatch
    ):
        """해외증거금 조회에서 일반/통합 주문가능 필드를 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "5856.200000",
                    "frcr_ord_psbl_amt1": "0.000000",
                    "frcr_gnrl_ord_psbl_amt": "5824.17",
                    "itgr_ord_psbl_amt": "5824.27",
                    "frcr_buy_amt_smtl": "0.00",
                    "tot_evlu_pfls_amt": "0.00",
                    "ovrs_tot_pfls": "0.00",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_overseas_margin(is_mock=False)

        assert len(result) == 1
        assert result[0]["natn_name"] == "미국"
        assert result[0]["crcy_cd"] == "USD"
        assert result[0]["frcr_dncl_amt1"] == 5856.2
        assert result[0]["frcr_ord_psbl_amt1"] == 0.0
        assert result[0]["frcr_gnrl_ord_psbl_amt"] == 5824.17
        assert result[0]["itgr_ord_psbl_amt"] == 5824.27

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_inquire_overseas_margin_safe_float_handles_blank_values(
        self, mock_client_class, monkeypatch
    ):
        """해외증거금 조회에서 빈 문자열/None을 0.0으로 안전하게 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output": [
                {
                    "natn_name": "미국",
                    "crcy_cd": "USD",
                    "frcr_dncl_amt1": "",
                    "frcr_ord_psbl_amt1": None,
                    "frcr_gnrl_ord_psbl_amt": "",
                    "itgr_ord_psbl_amt": None,
                    "frcr_buy_amt_smtl": "",
                    "tot_evlu_pfls_amt": None,
                    "ovrs_tot_pfls": "",
                }
            ],
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.brokers.kis.client.settings.kis_account_no",
            "12345678-01",
            raising=False,
        )

        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_overseas_margin(is_mock=False)

        assert len(result) == 1
        assert result[0]["frcr_dncl_amt1"] == 0.0
        assert result[0]["frcr_ord_psbl_amt1"] == 0.0
        assert result[0]["frcr_gnrl_ord_psbl_amt"] == 0.0
        assert result[0]["itgr_ord_psbl_amt"] == 0.0


class TestYahooService:
    """Test Yahoo Finance service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.yahoo.client.yf.download")
    async def test_fetch_ohlcv(self, mock_download, monkeypatch):
        """Test fetching OHLCV data from Yahoo Finance."""
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

        # Mock yfinance download response
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

        # Import and test the actual function
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
        """Test fetching current price from Yahoo Finance."""
        tracing_session = object()
        monkeypatch.setattr(
            "app.services.brokers.yahoo.client.build_yfinance_tracing_session",
            lambda: tracing_session,
        )

        # Mock Ticker instance
        mock_ticker = MagicMock()
        mock_ticker.fast_info.open = 150.0
        mock_ticker.fast_info.day_high = 155.0
        mock_ticker.fast_info.day_low = 145.0
        mock_ticker.fast_info.last_price = 152.0
        mock_ticker.fast_info.last_volume = 1000000
        mock_ticker_class.return_value = mock_ticker

        # Import and test the actual function
        from app.services.brokers.yahoo.client import fetch_price

        result = await fetch_price("AAPL")

        # 실제 반환되는 DataFrame 구조에 맞게 테스트 수정
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        # 'code'는 index로 설정되므로 columns에는 없음
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


class TestStockInfoServiceGuard:
    """Test guard conditions for stock info service."""

    @pytest.mark.asyncio
    async def test_process_buy_orders_enforces_one_percent_guard(self, monkeypatch):
        """process_buy_orders_with_analysis should stop when 1% 조건을 충족하지 못할 때."""

        async def fake_check(required_amount):
            return True, Decimal("200000")

        class DummyAsyncSession:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class DummyAnalysis:
            appropriate_buy_min = Decimal("90")
            appropriate_buy_max = Decimal("95")
            buy_hope_min = Decimal("92")
            buy_hope_max = Decimal("94")

        class DummyService:
            def __init__(self, db):
                self.db = db

            async def get_latest_analysis_by_symbol(self, symbol):
                return DummyAnalysis()

        from app.core.config import settings

        monkeypatch.setattr(
            "app.services.brokers.upbit.client.check_krw_balance_sufficient",
            fake_check,
        )
        monkeypatch.setattr(
            "app.core.db.AsyncSessionLocal", lambda: DummyAsyncSession()
        )
        monkeypatch.setattr(
            stock_info_service,
            "StockAnalysisService",
            DummyService,
        )
        monkeypatch.setattr(
            settings, "upbit_min_krw_balance", Decimal("10000"), raising=False
        )
        monkeypatch.setattr(
            settings, "upbit_buy_amount", Decimal("10000"), raising=False
        )

        calls = []

        async def fake_place(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "success": True,
                "message": "should not be returned",
                "orders_placed": 1,
                "total_amount": 10000.0,
            }

        monkeypatch.setattr(
            stock_info_service,
            "_place_multiple_buy_orders_by_analysis",
            fake_place,
        )

        result = await stock_info_service.process_buy_orders_with_analysis(
            symbol="KRW-ABC",
            current_price=100.0,
            avg_buy_price=100.0,
        )

        assert result["success"] is False
        assert "목표가" in result["message"]
        assert calls == []


class TestGeminiService:
    """Test Gemini AI service functionality."""

    @pytest.mark.asyncio
    @patch("redis.asyncio.Redis")
    async def test_model_rate_limiter(self, mock_redis):
        """Test Gemini model rate limiter."""
        # Mock Redis client
        mock_redis_client = AsyncMock()
        mock_redis.from_url.return_value = mock_redis_client
        mock_redis_client.get.return_value = None  # No rate limit

        # Import and test the actual class
        from app.core.model_rate_limiter import ModelRateLimiter

        limiter = ModelRateLimiter()

        # Test rate limit check
        result = await limiter.is_model_available("gemini-2.5-pro", "test_key")
        assert result is True


class TestDARTService:
    """Test DART disclosure service functionality."""

    def test_dart_service_import(self):
        """Test that DART service can be imported."""
        # This test verifies the module can be imported
        # Implementation depends on your actual DART service
        from app.services.disclosures import dart

        assert dart is not None


class TestKISIntegratedMarginParams:
    """Test KIS 통합증거금 요청 파라미터 검증 (OPSQ2001 방지)."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_params_includes_cma_field(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import (
            INTEGRATED_MARGIN_TR,
            INTEGRATED_MARGIN_URL,
            KISClient,
        )

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "950000",
                "stck_itgr_cash100_ord_psbl_amt": "900000",
            },
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        await client.inquire_integrated_margin()

        call_args = mock_client.get.call_args
        params = call_args.kwargs["params"]
        headers = call_args.kwargs["headers"]

        assert "CMA_EVLU_AMT_ICLD_YN" in params
        assert params["CMA_EVLU_AMT_ICLD_YN"] == "N"
        assert params["WCRC_FRCR_DVSN_CD"] == "01"
        assert params["FWEX_CTRT_FRCR_DVSN_CD"] == "01"
        assert "CANO" in params
        assert "ACNT_PRDT_CD" in params
        assert headers["tr_id"] == INTEGRATED_MARGIN_TR
        assert INTEGRATED_MARGIN_URL in call_args.args[0]

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_opsq2001_retry_with_y(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": "필수항목 누락: CMA_EVLU_AMT_ICLD_YN",
        }

        second_response = MagicMock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "850000",
                "stck_itgr_cash100_ord_psbl_amt": "800000",
            },
        }

        mock_client.get.side_effect = [first_response, second_response]

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.inquire_integrated_margin()

        assert mock_client.get.call_count == 2

        first_call_params = mock_client.get.call_args_list[0].kwargs["params"]
        assert first_call_params["CMA_EVLU_AMT_ICLD_YN"] == "N"

        second_call_params = mock_client.get.call_args_list[1].kwargs["params"]
        assert second_call_params["CMA_EVLU_AMT_ICLD_YN"] == "Y"

        assert result["dnca_tot_amt"] == 1000000.0
        assert result["stck_cash_objt_amt"] == 850000.0
        assert result["stck_itgr_cash100_ord_psbl_amt"] == 800000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_missing_domestic_fields_defaults_zero(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {
                "dnca_tot_amt": "1000000",
                "stck_cash_objt_amt": "",
                "stck_itgr_cash100_ord_psbl_amt": None,
            },
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.inquire_integrated_margin()

        assert result["stck_cash_objt_amt"] == 0.0
        assert result["stck_itgr_cash100_ord_psbl_amt"] == 0.0

    def test_extract_domestic_cash_summary_from_integrated_margin(self):
        from app.services.brokers.kis.client import (
            extract_domestic_cash_summary_from_integrated_margin,
        )

        summary = extract_domestic_cash_summary_from_integrated_margin(
            {
                "stck_cash_objt_amt": "777000",
                "stck_itgr_cash100_ord_psbl_amt": "555000",
                "raw": {
                    "stck_cash_objt_amt": "777000",
                    "stck_itgr_cash100_ord_psbl_amt": "555000",
                },
            }
        )

        assert summary["balance"] == 777000.0
        assert summary["orderable"] == 555000.0
        assert summary["raw"]["stck_cash_objt_amt"] == "777000"


class TestKISFailureLogging:
    """Test KIS API 실패 로깅 검증."""

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_domestic_cash_balance_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        """inquire_domestic_cash_balance 실패 시 endpoint, tr_id, 실제 key 이름 로깅."""
        import logging

        from app.services.brokers.kis.client import BALANCE_TR, BALANCE_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "TEST_ERROR",
            "msg1": "Test error message",
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.inquire_domestic_cash_balance()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "inquire_domestic_cash_balance" in error_log
        assert BALANCE_URL in error_log
        assert BALANCE_TR in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.brokers.kis.client import (
            INTEGRATED_MARGIN_TR,
            INTEGRATED_MARGIN_URL,
            KISClient,
        )

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OTHER_ERROR",
            "msg1": "Some other error",
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.inquire_integrated_margin()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "inquire_integrated_margin" in error_log
        assert INTEGRATED_MARGIN_URL in error_log
        assert INTEGRATED_MARGIN_TR in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log
        assert "CMA_EVLU_AMT_ICLD_YN" in error_log
        assert "OTHER_ERROR" in error_log

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_msg1_none_no_typeerror(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": None,
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with pytest.raises(RuntimeError) as exc_info:
            await client.inquire_integrated_margin()

        assert "OPSQ2001" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_integrated_margin_opsq2001_cma_warning_logged(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        first_response = MagicMock()
        first_response.status_code = 200
        first_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": "CMA_EVLU_AMT_ICLD_YN 파라미터 오류입니다.",
        }

        second_response = MagicMock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "rt_cd": "0",
            "output1": {"dnca_tot_amt": "500000"},
        }

        mock_client.get.side_effect = [first_response, second_response]

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.WARNING):
            result = await client.inquire_integrated_margin()

        assert any(
            "OPSQ2001" in record.message and "CMA_EVLU_AMT_ICLD_YN" in record.message
            for record in caplog.records
            if record.levelno == logging.WARNING
        )
        assert result["dnca_tot_amt"] == 500000.0

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_order_korea_stock_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        """order_korea_stock 실패 시 endpoint, tr_id, request_keys 로깅."""
        import logging

        from app.services.brokers.kis.client import KOREA_ORDER_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "ORDER_ERROR",
            "msg1": "Order failed",
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await client.order_korea_stock(
                    stock_code="005930",
                    order_type="buy",
                    quantity=10,
                    price=80000,
                )

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_logs) >= 1

        error_log = error_logs[0].message
        assert "order_korea_stock" in error_log
        assert KOREA_ORDER_URL in error_log
        assert "CANO" in error_log
        assert "ACNT_PRDT_CD" in error_log
        assert "PDNO" in error_log
        assert "ORD_QTY" in error_log
        assert "ORD_UNPR" in error_log
        assert "EXCG_ID_DVSN_CD" in error_log

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("order_type", "is_mock", "expected_tr_id"),
        [
            ("buy", False, "TTTC0012U"),
            ("buy", True, "VTTC0012U"),
            ("sell", False, "TTTC0011U"),
            ("sell", True, "VTTC0011U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_order_korea_stock_uses_new_tr_and_sor(
        self,
        mock_settings,
        mock_client_class,
        order_type,
        is_mock,
        expected_tr_id,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "1", "ORD_TMD": "100000"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.order_korea_stock(
            stock_code="005930",
            order_type=order_type,
            quantity=3,
            price=81000,
            is_mock=is_mock,
        )

        assert result["odno"] == "1"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_mock", "expected_tr_id"),
        [
            (False, "TTTC0013U"),
            (True, "VTTC0013U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_cancel_korea_order_uses_new_tr_sor_and_explicit_orgno(
        self,
        mock_settings,
        mock_client_class,
        is_mock,
        expected_tr_id,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "2", "ORD_TMD": "100100"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")

        result = await client.cancel_korea_order(
            order_number="10001",
            stock_code="005930",
            quantity=3,
            price=81000,
            order_type="buy",
            krx_fwdg_ord_orgno="06010",
            is_mock=is_mock,
        )

        assert result["odno"] == "2"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"
        assert body["KRX_FWDG_ORD_ORGNO"] == "06010"
        assert body["RVSE_CNCL_DVSN_CD"] == "02"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_mock", "expected_tr_id"),
        [
            (False, "TTTC0013U"),
            (True, "VTTC0013U"),
        ],
    )
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_modify_korea_order_uses_new_tr_sor_and_resolved_orgno(
        self,
        mock_settings,
        mock_client_class,
        is_mock,
        expected_tr_id,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "0",
            "msg1": "ok",
            "output": {"ODNO": "3", "ORD_TMD": "100200"},
        }
        mock_client.post.return_value = mock_response

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")
        client.inquire_korea_orders = AsyncMock(
            return_value=[
                {
                    "ord_no": "10002",
                    "pdno": "005930",
                    "ord_gno_brno": "06010",
                }
            ]
        )

        result = await client.modify_korea_order(
            order_number="10002",
            stock_code="005930",
            quantity=4,
            new_price=81500,
            is_mock=is_mock,
        )

        assert result["odno"] == "3"

        headers = mock_client.post.call_args.kwargs["headers"]
        body = mock_client.post.call_args.kwargs["json"]

        assert headers["tr_id"] == expected_tr_id
        assert body["EXCG_ID_DVSN_CD"] == "SOR"
        assert body["KRX_FWDG_ORD_ORGNO"] == "06010"
        assert body["RVSE_CNCL_DVSN_CD"] == "01"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_cancel_korea_order_raises_when_orgno_resolution_fails(
        self,
        mock_settings,
        mock_client_class,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        client = KISClient()
        client._token_manager = AsyncMock()
        client._token_manager.get_token = AsyncMock(return_value="test_token")
        client.inquire_korea_orders = AsyncMock(
            return_value=[
                {
                    "ord_no": "other-order",
                    "pdno": "005930",
                    "ord_gno_brno": "06010",
                }
            ]
        )

        with pytest.raises(
            ValueError,
            match="KRX_FWDG_ORD_ORGNO not found for order 10001",
        ):
            await client.cancel_korea_order(
                order_number="10001",
                stock_code="005930",
                quantity=3,
                price=81000,
                order_type="buy",
                is_mock=False,
            )

        mock_client.post.assert_not_called()


class TestKISOverseasDailyPrice:
    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_daily_price_parses_output2(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "xymd": "20260102",
                    "open": "190.5",
                    "high": "193.0",
                    "low": "189.8",
                    "clos": "192.2",
                    "tvol": "1000",
                },
                {
                    "xymd": "20260103",
                    "open": "192.3",
                    "high": "194.1",
                    "low": "191.0",
                    "clos": "193.8",
                    "tvol": "1200",
                },
            ],
        }
        mock_client.get.return_value = mock_response

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()

        result = await client.inquire_overseas_daily_price(symbol="AAPL", n=2)

        assert len(result) == 2
        assert list(result.columns) == [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
        assert float(result.iloc[-1]["close"]) == 193.8

        params = mock_client.get.call_args.kwargs["params"]
        assert params["GUBN"] == "0"
        assert params["SYMB"] == "AAPL"

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    @patch("app.services.brokers.kis.client.settings")
    async def test_inquire_overseas_daily_price_retries_on_expired_token(
        self, mock_settings, mock_client_class
    ):
        from app.services.brokers.kis.client import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        expired_response = MagicMock()
        expired_response.status_code = 200
        expired_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        }

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {
                    "xymd": "20260103",
                    "open": "192.3",
                    "high": "194.1",
                    "low": "191.0",
                    "clos": "193.8",
                    "tvol": "1200",
                }
            ],
        }

        mock_client.get.side_effect = [expired_response, success_response]

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._token_manager = AsyncMock()
        client._token_manager.clear_token = AsyncMock(return_value=None)

        result = await client.inquire_overseas_daily_price(symbol="AAPL", n=1)

        assert len(result) == 1
        assert mock_client.get.call_count == 2
        client._token_manager.clear_token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exchange_code", "expected_excd"),
    [("NASD", "NAS"), ("NYSE", "NYS"), ("AMEX", "AMS")],
)
async def test_kis_inquire_overseas_minute_chart_maps_exchange_codes_and_returns_empty_page(
    monkeypatch,
    exchange_code,
    expected_excd,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={"rt_cd": "0", "output1": {"next": "", "more": "N"}, "output2": []}
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart(
        "BRK.B", exchange_code=exchange_code
    )

    assert page.frame.empty
    assert list(page.frame.columns) == [
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    assert page.has_more is False
    assert page.next_keyb is None

    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.args[0] == "GET"
    assert await_args.args[1].endswith("/inquire-time-itemchartprice")
    assert await_args.kwargs["tr_id"] == "HHDFS76950200"
    assert await_args.kwargs["api_name"] == "inquire_overseas_minute_chart"

    params = await_args.kwargs["params"]
    assert params["AUTH"] == ""
    assert params["EXCD"] == expected_excd
    assert params["SYMB"] == "BRK/B"
    assert params["NMIN"] == "1"
    assert params["PINC"] == "1"
    assert params["NEXT"] == ""
    assert params["NREC"] == "120"
    assert params["FILL"] == ""
    assert params["KEYB"] == ""


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_marks_continuation_when_keyb_given(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={"rt_cd": "0", "output1": {"next": "", "more": "N"}, "output2": []}
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    await client.inquire_overseas_minute_chart(
        "AAPL", exchange_code="NASD", keyb="20260219100000"
    )

    await_args = request_mock.await_args
    assert await_args is not None
    params = await_args.kwargs["params"]
    assert params["NEXT"] == "1"
    assert params["KEYB"] == "20260219100000"


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_parses_rows(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "180.1",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "evol": "100",
                    "eamt": "18050",
                },
                {
                    "xymd": "20260219",
                    "xhms": "093100",
                    "open": "180.5",
                    "high": "180.7",
                    "low": "180.2",
                    "clos": "180.4",
                    "evol": "80",
                    "eamt": "14432",
                },
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert list(page.frame.columns) == [
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    assert len(page.frame) == 2
    assert list(page.frame["close"]) == [180.5, 180.4]
    assert list(page.frame["volume"]) == [100, 80]
    assert list(page.frame["value"]) == [18050, 14432]
    assert page.frame.iloc[0]["datetime"] == pd.Timestamp("2026-02-19 09:30:00")
    assert page.frame.iloc[0]["date"] == date(2026, 2, 19)
    assert page.frame.iloc[0]["time"].isoformat() == "09:30:00"


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_falls_back_to_tvol_and_tamt(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "180.1",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "tvol": "101",
                    "tamt": "18230",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert list(page.frame["volume"]) == [101]
    assert list(page.frame["value"]) == [18230]


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_falsy_string_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": "",
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_invalid_numeric_value(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "093000",
                    "open": "bad-open",
                    "high": "181.0",
                    "low": "179.8",
                    "last": "180.5",
                    "evol": "100",
                    "eamt": "18050",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="invalid numeric field open"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["EGW00123", "EGW00121"])
async def test_kis_inquire_overseas_minute_chart_retries_on_expired_token(
    monkeypatch,
    error_code,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    ensure_token = AsyncMock()
    monkeypatch.setattr(client, "_ensure_token", ensure_token)
    request_mock = AsyncMock(
        side_effect=[
            {"rt_cd": "1", "msg_cd": error_code, "msg1": "token expired"},
            {
                "rt_cd": "0",
                "output1": {"next": "", "more": "N"},
                "output2": [
                    {
                        "xymd": "20260219",
                        "xhms": "093000",
                        "open": "180.1",
                        "high": "181.0",
                        "low": "179.8",
                        "last": "180.5",
                        "evol": "100",
                        "eamt": "18050",
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)
    client._token_manager = AsyncMock()
    client._token_manager.clear_token = AsyncMock(return_value=None)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert len(page.frame) == 1
    assert request_mock.await_count == 2
    assert ensure_token.await_count == 2
    client._token_manager.clear_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_raises_controlled_error_on_non_list_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "", "more": "N"},
            "output2": {"foo": "bar"},
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_overseas_minute_chart("AAPL")


@pytest.mark.asyncio
async def test_kis_inquire_overseas_minute_chart_computes_next_keyb_from_oldest_row(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output1": {"next": "Y", "more": "Y"},
            "output2": [
                {
                    "xymd": "20260219",
                    "xhms": "100200",
                    "open": "180.6",
                    "high": "180.8",
                    "low": "180.4",
                    "last": "180.7",
                    "evol": "110",
                    "eamt": "19877",
                },
                {
                    "xymd": "20260219",
                    "xhms": "100100",
                    "open": "180.5",
                    "high": "180.6",
                    "low": "180.3",
                    "last": "180.4",
                    "evol": "90",
                    "eamt": "16236",
                },
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    page = await client.inquire_overseas_minute_chart("AAPL")

    assert page.has_more is True
    assert page.next_keyb == "20260219100000"


@pytest.mark.asyncio
async def test_kis_inquire_time_dailychartprice_parses_rows(monkeypatch):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260219",
                    "stck_cntg_hour": "100000",
                    "stck_oprc": "70000",
                    "stck_hgpr": "70200",
                    "stck_lwpr": "69900",
                    "stck_prpr": "70100",
                    "cntg_vol": "100",
                    "acml_tr_pbmn": "7010000",
                }
            ],
        }
    )
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        request_mock,
    )

    df = await client.inquire_time_dailychartprice("005930", market="UN", n=1)

    assert len(df) == 1
    assert {"datetime", "open", "high", "low", "close", "volume", "value"} <= set(
        df.columns
    )
    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.args[0] == "GET"
    assert await_args.args[1].endswith("/inquire-time-dailychartprice")
    assert await_args.kwargs["tr_id"] == "FHKST03010230"
    assert await_args.kwargs["api_name"] == "inquire_time_dailychartprice"
    assert await_args.kwargs["params"]["FID_FAKE_TICK_INCU_YN"] == ""
    assert "FID_INPUT_DATE_2" not in await_args.kwargs["params"]
    assert "FID_INPUT_TIME_1" not in await_args.kwargs["params"]
    assert "FID_INPUT_TIME_2" not in await_args.kwargs["params"]


@pytest.mark.asyncio
async def test_kis_inquire_time_dailychartprice_uses_end_time_when_provided(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        request_mock,
    )

    await client.inquire_time_dailychartprice(
        "005930",
        market="J",
        n=1,
        end_date=pd.Timestamp("2026-02-19"),
        end_time="153000",
    )

    request_mock.assert_awaited_once()
    await_args = request_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["params"]["FID_COND_MRKT_DIV_CODE"] == "J"
    assert await_args.kwargs["params"]["FID_INPUT_DATE_1"] == "20260219"
    assert await_args.kwargs["params"]["FID_INPUT_HOUR_1"] == "153000"


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_returns_empty_dataframe_on_empty_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": []})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    df = await client.inquire_daily_itemchartprice("005930", market="UN", n=5)

    assert df.empty
    assert list(df.columns) == [
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]
    request_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_raises_controlled_error_on_missing_date(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(
        return_value={
            "rt_cd": "0",
            "output2": [
                {
                    "stck_oprc": "70000",
                    "stck_hgpr": "70200",
                    "stck_lwpr": "69900",
                    "stck_clpr": "70100",
                    "acml_vol": "100",
                    "acml_tr_pbmn": "7010000",
                }
            ],
        }
    )
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="stck_bsop_date"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=1)


@pytest.mark.asyncio
async def test_kis_inquire_daily_itemchartprice_raises_controlled_error_on_non_list_payload(
    monkeypatch,
):
    from app.services.brokers.kis.client import KISClient

    client = KISClient()
    monkeypatch.setattr(client, "_ensure_token", AsyncMock())
    request_mock = AsyncMock(return_value={"rt_cd": "0", "output2": {"foo": "bar"}})
    monkeypatch.setattr(client, "_request_with_rate_limit", request_mock)

    with pytest.raises(RuntimeError, match="expected list"):
        await client.inquire_daily_itemchartprice("005930", market="UN", n=1)


def test_aggregate_to_hourly_keeps_partial_bucket():
    from app.services.brokers.kis.client import KISClient

    df = pd.DataFrame(
        {
            "datetime": pd.to_datetime(["2026-02-19 10:10:00", "2026-02-19 10:20:00"]),
            "open": [1, 2],
            "high": [3, 4],
            "low": [1, 2],
            "close": [2, 3],
            "volume": [10, 20],
            "value": [100, 200],
        }
    )

    out = KISClient._aggregate_intraday_to_hour(df)

    assert len(out) == 1
    assert out.iloc[0]["close"] == 3


class TestKISRequestWithRateLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "params", "json_body", "expected_call"),
        [
            ("GET", {"foo": "bar"}, None, "get"),
            ("POST", None, {"foo": "bar"}, "post"),
        ],
    )
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_passes_timeout_keyword(
        self,
        mock_client_class,
        mock_get_limiter,
        method,
        params,
        json_body,
        expected_call,
    ):
        """_request_with_rate_limit calls httpx with explicit timeout keyword."""
        from app.services.brokers.kis.client import KISClient

        timeout_value = 7.5
        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"rt_cd": "0", "output": []}

        mock_client = AsyncMock()
        if expected_call == "get":
            mock_client.get.return_value = mock_response
        else:
            mock_client.post.return_value = mock_response

        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        result = await client._request_with_rate_limit(
            method,
            "https://example.com/uapi/domestic-stock/v1/quotations/inquire-price",
            headers={"authorization": "Bearer token"},
            params=params,
            json_body=json_body,
            timeout=timeout_value,
            api_name="test_api",
            tr_id="TEST123",
        )

        assert result == {"rt_cd": "0", "output": []}
        mock_get_limiter.assert_awaited_once()
        mock_client_class.assert_called_once_with(timeout=timeout_value)

        request_call = getattr(mock_client, expected_call)
        request_call.assert_awaited_once()
        assert request_call.await_args.kwargs["timeout"] == timeout_value

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_returns_json_body_on_http_500(
        self,
        mock_client_class,
        mock_get_limiter,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        }

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        result = await client._request_with_rate_limit(
            "GET",
            "https://example.com/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            headers={"authorization": "Bearer token"},
            params={"FID_INPUT_ISCD": "005930"},
            timeout=5.0,
            api_name="inquire_orderbook",
            tr_id="FHKST01010200",
        )

        assert result["msg_cd"] == "EGW00123"
        mock_response.raise_for_status.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.brokers.kis.client.get_limiter")
    @patch("app.services.brokers.kis.base.httpx.AsyncClient")
    async def test_request_with_rate_limit_raises_http_error_on_http_500_non_json(
        self,
        mock_client_class,
        mock_get_limiter,
    ):
        from app.services.brokers.kis.client import KISClient

        mock_limiter = AsyncMock()
        mock_get_limiter.return_value = mock_limiter

        request = httpx.Request("GET", "https://example.com/failing")
        status_error = httpx.HTTPStatusError(
            "Server Error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        mock_response.json.side_effect = ValueError("invalid json")
        mock_response.raise_for_status.side_effect = status_error

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        client = KISClient()
        with pytest.raises(httpx.HTTPStatusError):
            await client._request_with_rate_limit(
                "GET",
                "https://example.com/failing",
                headers={"authorization": "Bearer token"},
                params={"FID_INPUT_ISCD": "005930"},
                timeout=5.0,
                api_name="inquire_orderbook",
                tr_id="FHKST01010200",
            )


class TestKISInquireOrderbook:
    @pytest.mark.asyncio
    async def test_inquire_orderbook_returns_output1_payload(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "0",
                "output1": {"askp1": "70100", "askp_rsqn1": "111"},
            }
        )

        result = await client.inquire_orderbook("005930")
        assert result == {"askp1": "70100", "askp_rsqn1": "111"}

    @pytest.mark.asyncio
    async def test_inquire_orderbook_fallbacks_to_output_payload(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={
                "rt_cd": "0",
                "output": {"askp1": "70100", "askp_rsqn1": "111"},
            }
        )

        result = await client.inquire_orderbook("005930")
        assert result == {"askp1": "70100", "askp_rsqn1": "111"}

    @pytest.mark.asyncio
    async def test_inquire_orderbook_raises_when_output_payload_missing(self):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        client._ensure_token = AsyncMock(return_value=None)
        client._request_with_rate_limit = AsyncMock(
            return_value={"rt_cd": "0", "msg_cd": "0", "msg1": "ok"}
        )

        with pytest.raises(RuntimeError, match="output1"):
            await client.inquire_orderbook("005930")


class TestKISRateLimitLookup:
    @pytest.mark.parametrize(
        ("api_key", "expected_rate", "expected_period"),
        [
            (
                "FHKST03010100|/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                20,
                1.0,
            ),
            (
                "FHKST03010230|/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
                20,
                1.0,
            ),
            (
                "TTTC8434R|/uapi/domestic-stock/v1/trading/inquire-balance",
                10,
                1.0,
            ),
            (
                "TTTC8001R|/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
                10,
                1.0,
            ),
            (
                "TTTC8036R|/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
                10,
                1.0,
            ),
        ],
    )
    def test_get_rate_limit_for_seeded_api_keys(
        self, api_key: str, expected_rate: int, expected_period: float
    ):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()

        assert client._get_rate_limit_for_api(api_key) == (
            expected_rate,
            expected_period,
        )

    def test_get_rate_limit_for_unknown_api_key_warns_once_and_falls_back(self, caplog):
        from app.services.brokers.kis.client import KISClient

        client = KISClient()
        api_key = "UNKNOWN|/uapi/test"

        with caplog.at_level(logging.WARNING):
            first = client._get_rate_limit_for_api(api_key)
            second = client._get_rate_limit_for_api(api_key)

        assert first == (19, 1.0)
        assert second == (19, 1.0)
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and api_key in record.message
        ]
        assert len(warnings) == 1
