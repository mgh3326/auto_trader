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


class TestCeleryTaskFailureHandler:
    """Test Celery task failure signal handler."""

    def test_handle_task_failure_registered(self):
        """task_failure 핸들러가 올바르게 등록되어 있는지 확인."""
        from celery.signals import task_failure

        # task_failure 시그널에 핸들러가 등록되었는지 확인

        # task_failure 시그널의 receivers 확인
        receivers = task_failure.receivers
        # weakref로 저장되므로 문자열에서 함수 이름 확인
        handler_found = any("handle_task_failure" in str(r[1]) for r in receivers)

        assert handler_found, f"handle_task_failure not found in receivers: {receivers}"

    def test_handle_task_failure_with_disabled_reporter(self):
        """ErrorReporter가 비활성화 상태일 때 핸들러가 안전하게 동작하는지 확인."""
        from app.core.celery_app import handle_task_failure
        from app.monitoring.error_reporter import get_error_reporter

        # ErrorReporter가 비활성화 상태인지 확인 (테스트 환경에서는 기본적으로 비활성화)
        get_error_reporter()  # verify it doesn't raise

        # 예외 없이 실행되어야 함
        test_exception = ValueError("Test error message")

        # Mock sender 생성
        class MockSender:
            name = "test.mock_task"

        # 핸들러 직접 호출 - 예외 없이 완료되어야 함
        handle_task_failure(
            sender=MockSender,
            task_id="test-task-id-123",
            exception=test_exception,
            args=("arg1", "arg2"),
            kwargs={"key": "value"},
            traceback=None,
            einfo=None,
        )

    @patch("app.monitoring.error_reporter.get_error_reporter")
    def test_handle_task_failure_sends_telegram_when_enabled(self, mock_get_reporter):
        """ErrorReporter가 활성화 상태일 때 Telegram 알림이 전송되는지 확인."""
        from app.core.celery_app import handle_task_failure

        # Mock ErrorReporter 설정
        mock_reporter = MagicMock()
        mock_reporter._enabled = True
        mock_reporter.send_error_to_telegram = AsyncMock(return_value=True)
        mock_get_reporter.return_value = mock_reporter

        test_exception = TypeError("unexpected keyword argument 'symbol'")

        class MockSender:
            name = "kis.run_per_domestic_stock_automation"

        # 핸들러 호출
        handle_task_failure(
            sender=MockSender,
            task_id="celery-task-abc123",
            exception=test_exception,
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
        )

        # send_error_to_telegram이 호출되었는지 확인
        mock_reporter.send_error_to_telegram.assert_called_once()

        # 호출 인자 확인
        call_args = mock_reporter.send_error_to_telegram.call_args
        assert call_args.kwargs["error"] == test_exception
        assert "task_name" in call_args.kwargs["additional_context"]
        assert (
            call_args.kwargs["additional_context"]["task_name"]
            == "kis.run_per_domestic_stock_automation"
        )
        assert call_args.kwargs["additional_context"]["task_id"] == "celery-task-abc123"
