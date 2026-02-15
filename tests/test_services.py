"""
Tests for service modules.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from app.services import stock_info_service
from app.services.upbit import fetch_ohlcv, fetch_price


class TestUpbitService:
    """Test Upbit service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.upbit._request_json")
    async def test_fetch_ohlcv(self, mock_request):
        """Test fetching OHLCV data."""
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
    @patch("app.services.upbit._request_json")
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


class TestKISService:
    """Test KIS service functionality."""

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_kis_client_initialization(self, mock_client_class):
        """Test KIS client initialization."""
        # Mock client
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Import and test the actual class
        from app.services.kis import KISClient

        client = KISClient()

        assert client is not None
        assert hasattr(client, "_hdr_base")

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_kis_volume_rank(self, mock_client_class):
        """Test KIS volume rank functionality."""
        # Mock client
        mock_client = AsyncMock()

        # Mock response - r.json()이 동기적으로 동작하도록 설정
        mock_response = MagicMock()
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

        # Mock client class가 context manager로 동작하도록 설정
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None

        # Import and test the actual function
        from app.services.kis import KISClient

        client = KISClient()

        # Mock the token loading
        with patch.object(client, "_ensure_token"):
            result = await client.volume_rank()

            assert isinstance(result, list)
            assert len(result) > 0

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_fetch_my_stocks_inqr_dvsn_domestic(self, mock_client_class):
        """Verify INQR_DVSN is set to '00' for domestic stock queries."""
        # Setup mock client and response
        mock_client = AsyncMock()

        # Mock response for empty holdings (end of pagination)
        mock_response = MagicMock()
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
        from app.services.kis import KISClient

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
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_success(
        self, mock_client_class, monkeypatch
    ):
        """inquire-balance(output2)에서 국내 현금 잔고를 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
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
            "app.services.kis.settings.kis_account_no", "12345678-01", raising=False
        )

        from app.services.kis import KISClient

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
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_fallback_ord_psbl_cash(
        self, mock_client_class, monkeypatch
    ):
        """stck_cash_ord_psbl_amt가 없으면 ord_psbl_cash를 fallback으로 사용한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
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
            "app.services.kis.settings.kis_account_no", "12345678-01", raising=False
        )

        from app.services.kis import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            result = await client.inquire_domestic_cash_balance(is_mock=False)

        assert result["dnca_tot_amt"] == 1140000.0
        assert result["stck_cash_ord_psbl_amt"] == 950000.0

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_inquire_domestic_cash_balance_api_error_raises(
        self, mock_client_class, monkeypatch
    ):
        """API 오류 응답은 RuntimeError로 전달한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "EGW99999",
            "msg1": "failure",
        }
        mock_client.get.return_value = mock_response
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client_class.return_value.__aexit__.return_value = None
        monkeypatch.setattr(
            "app.services.kis.settings.kis_account_no", "12345678-01", raising=False
        )

        from app.services.kis import KISClient

        client = KISClient()
        with patch.object(client, "_ensure_token"):
            with pytest.raises(RuntimeError, match="EGW99999"):
                await client.inquire_domestic_cash_balance(is_mock=False)

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_inquire_overseas_margin_parses_extended_orderable_fields(
        self, mock_client_class, monkeypatch
    ):
        """해외증거금 조회에서 일반/통합 주문가능 필드를 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
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
            "app.services.kis.settings.kis_account_no", "12345678-01", raising=False
        )

        from app.services.kis import KISClient

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
    @patch("app.services.kis.httpx.AsyncClient")
    async def test_inquire_overseas_margin_safe_float_handles_blank_values(
        self, mock_client_class, monkeypatch
    ):
        """해외증거금 조회에서 빈 문자열/None을 0.0으로 안전하게 파싱한다."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
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
            "app.services.kis.settings.kis_account_no", "12345678-01", raising=False
        )

        from app.services.kis import KISClient

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
    @patch("app.services.yahoo.yf.download")
    async def test_fetch_ohlcv(self, mock_download):
        """Test fetching OHLCV data from Yahoo Finance."""
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
        from app.services.yahoo import fetch_ohlcv

        result = await fetch_ohlcv("AAPL", days=3)

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        assert "date" in result.columns
        assert "open" in result.columns
        assert "close" in result.columns

    @pytest.mark.asyncio
    @patch("app.services.yahoo.yf.Ticker")
    async def test_fetch_price(self, mock_ticker_class):
        """Test fetching current price from Yahoo Finance."""
        # Mock Ticker instance
        mock_ticker = MagicMock()
        mock_ticker.fast_info.open = 150.0
        mock_ticker.fast_info.day_high = 155.0
        mock_ticker.fast_info.day_low = 145.0
        mock_ticker.fast_info.last_price = 152.0
        mock_ticker.fast_info.last_volume = 1000000
        mock_ticker_class.return_value = mock_ticker

        # Import and test the actual function
        from app.services.yahoo import fetch_price

        result = await fetch_price("AAPL")

        # 실제 반환되는 DataFrame 구조에 맞게 테스트 수정
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1
        # 'code'는 index로 설정되므로 columns에는 없음
        assert "date" in result.columns
        assert "close" in result.columns


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
            "app.services.upbit.check_krw_balance_sufficient",
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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_integrated_margin_params_includes_cma_field(
        self, mock_settings, mock_client_class
    ):
        from app.services.kis import (
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
        mock_response.json.return_value = {
            "rt_cd": "0",
            "output1": {"dnca_tot_amt": "1000000"},
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
        assert "CANO" in params
        assert "ACNT_PRDT_CD" in params
        assert headers["tr_id"] == INTEGRATED_MARGIN_TR
        assert INTEGRATED_MARGIN_URL in call_args.args[0]

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_integrated_margin_opsq2001_retry_with_y(
        self, mock_settings, mock_client_class
    ):
        from app.services.kis import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        first_response = MagicMock()
        first_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": "필수항목 누락: CMA_EVLU_AMT_ICLD_YN",
        }

        second_response = MagicMock()
        second_response.json.return_value = {
            "rt_cd": "0",
            "output1": {"dnca_tot_amt": "1000000"},
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


class TestKISFailureLogging:
    """Test KIS API 실패 로깅 검증."""

    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_domestic_cash_balance_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        """inquire_domestic_cash_balance 실패 시 endpoint, tr_id, 실제 key 이름 로깅."""
        import logging

        from app.services.kis import BALANCE_TR, BALANCE_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_integrated_margin_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.kis import (
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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_integrated_margin_msg1_none_no_typeerror(
        self, mock_settings, mock_client_class
    ):
        from app.services.kis import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_integrated_margin_opsq2001_cma_warning_logged(
        self, mock_settings, mock_client_class, caplog
    ):
        import logging

        from app.services.kis import KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        first_response = MagicMock()
        first_response.json.return_value = {
            "rt_cd": "1",
            "msg_cd": "OPSQ2001",
            "msg1": "CMA_EVLU_AMT_ICLD_YN 파라미터 오류입니다.",
        }

        second_response = MagicMock()
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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_order_korea_stock_logs_failure_details(
        self, mock_settings, mock_client_class, caplog
    ):
        """order_korea_stock 실패 시 endpoint, tr_id, request_keys 로깅."""
        import logging

        from app.services.kis import KOREA_ORDER_URL, KISClient

        mock_settings.kis_account_no = "1234567890"
        mock_settings.kis_app_key = "test_key"
        mock_settings.kis_app_secret = "test_secret"
        mock_settings.kis_access_token = "test_token"

        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
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


class TestKISOverseasDailyPrice:
    @pytest.mark.asyncio
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_overseas_daily_price_parses_output2(
        self, mock_settings, mock_client_class
    ):
        from app.services.kis import KISClient

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
    @patch("app.services.kis.httpx.AsyncClient")
    @patch("app.services.kis.settings")
    async def test_inquire_overseas_daily_price_retries_on_expired_token(
        self, mock_settings, mock_client_class
    ):
        from app.services.kis import KISClient

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


class TestKISRequestWithRateLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "params", "json_body", "expected_call"),
        [
            ("GET", {"foo": "bar"}, None, "get"),
            ("POST", None, {"foo": "bar"}, "post"),
        ],
    )
    @patch("app.services.kis.get_limiter")
    @patch("app.services.kis.httpx.AsyncClient")
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
        from app.services.kis import KISClient

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
