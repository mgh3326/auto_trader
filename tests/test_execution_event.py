"""
Unit tests for execution event publisher

Tests for Redis execution event publishing including:
- Channel routing by market (kr/us)
- JSON serialization (datetime/Decimal)
- Redis connection handling
- Graceful shutdown
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.services import execution_event as execution_event_module
from app.services.execution_event import (
    _serialize_for_redis,
    close_redis,
    publish_execution_event,
)


@pytest.fixture(autouse=True)
def reset_redis_client():
    """Reset global _redis_client between tests to prevent state leakage"""
    original_client = execution_event_module._redis_client
    execution_event_module._redis_client = None
    yield
    execution_event_module._redis_client = original_client


@pytest.mark.unit
class TestExecutionEventSerialization:
    """Tests for event data serialization"""

    def test_serialize_datetime(self):
        """datetime 객체 ISO 포맷 변환 테스트"""
        test_datetime = datetime(2026, 2, 12, 10, 47, 27)
        result = _serialize_for_redis(test_datetime)

        assert result == "2026-02-12T10:47:27"

    def test_serialize_decimal(self):
        """Decimal 객체 float 변환 테스트"""
        test_decimal = Decimal("12345.67")
        result = _serialize_for_redis(test_decimal)

        assert result == 12345.67

    def test_serialize_string(self):
        """문자열 그대로 반환 테스트"""
        test_string = "test_value"
        result = _serialize_for_redis(test_string)

        assert result == "test_value"

    def test_serialize_integer(self):
        """정수 그대로 반환 테스트"""
        test_int = 42
        result = _serialize_for_redis(test_int)

        assert result == 42

    def test_serialize_dict(self):
        """딕셔너리 재귀 직렬화 테스트"""
        test_dict = {
            "timestamp": datetime(2026, 2, 12, 10, 47, 27),
            "price": Decimal("100.50"),
            "symbol": "AAPL",
        }
        result = _serialize_for_redis(test_dict)

        assert result == {
            "timestamp": "2026-02-12T10:47:27",
            "price": 100.50,
            "symbol": "AAPL",
        }

    def test_serialize_list(self):
        """리스트 재귀 직렬화 테스트"""
        test_list = [
            Decimal("100.50"),
            Decimal("200.75"),
            datetime(2026, 2, 12, 10, 47, 27),
        ]
        result = _serialize_for_redis(test_list)

        assert result == [100.50, 200.75, "2026-02-12T10:47:27"]


@pytest.mark.unit
class TestExecutionEventChannelRouting:
    """Tests for channel routing by market"""

    @pytest.mark.asyncio
    async def test_publish_kr_market_event(self):
        """국내(kr) 시장 채널 발행 테스트"""
        event = {
            "type": "execution",
            "market": "kr",
            "symbol": "005930",
            "order_id": "ORDER-123",
        }

        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        with patch(
            "app.services.execution_event._get_redis_client", return_value=mock_redis
        ):
            await publish_execution_event(event)

            mock_redis.publish.assert_called_once()
            call_args = mock_redis.publish.call_args[0]
            assert call_args[0] == "execution:kr"

    @pytest.mark.asyncio
    async def test_publish_us_market_event(self):
        """해외(us) 시장 채널 발행 테스트"""
        event = {
            "type": "execution",
            "market": "us",
            "symbol": "AAPL",
            "order_id": "ORDER-456",
        }

        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        with patch(
            "app.services.execution_event._get_redis_client", return_value=mock_redis
        ):
            await publish_execution_event(event)

            call_args = mock_redis.publish.call_args[0]
            assert call_args[0] == "execution:us"

    @pytest.mark.asyncio
    async def test_publish_unknown_market_event(self):
        """알수지 않은 시장 채널 발행 테스트"""
        event = {
            "type": "execution",
            "market": "unknown",
            "symbol": "BTC",
        }

        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        with patch(
            "app.services.execution_event._get_redis_client", return_value=mock_redis
        ):
            await publish_execution_event(event)

            call_args = mock_redis.publish.call_args[0]
            assert call_args[0] == "execution:unknown"


@pytest.mark.unit
class TestExecutionEventWithDCA:
    """Tests for execution events with DCA next step"""

    @pytest.mark.asyncio
    async def test_publish_event_with_dca_next_step(self):
        """DCA 다음 단계 포함 이벤트 발행 테스트"""
        event = {
            "type": "execution",
            "market": "kr",
            "symbol": "005930",
            "order_id": "ORDER-123",
            "dca_next_step": {
                "plan_id": 1,
                "step_number": 2,
                "target_price": Decimal("49000.00"),
                "target_quantity": Decimal("0.002"),
            },
        }

        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()

        with patch(
            "app.services.execution_event._get_redis_client", return_value=mock_redis
        ):
            await publish_execution_event(event)

            call_args = mock_redis.publish.call_args[0]
            serialized_message = call_args[1]

            import json

            parsed = json.loads(serialized_message)
            assert "dca_next_step" in parsed
            assert parsed["dca_next_step"]["plan_id"] == 1
            assert parsed["dca_next_step"]["step_number"] == 2
            assert parsed["dca_next_step"]["target_price"] == 49000.0


@pytest.mark.unit
class TestExecutionEventErrorHandling:
    """Tests for error handling"""

    @pytest.mark.asyncio
    async def test_publish_with_redis_error(self):
        """Redis 발행 실패 시 에러 처리 테스트 - 원 예외 재발행"""
        event = {"type": "execution", "market": "kr"}

        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock(side_effect=Exception("Redis connection error"))

        with patch(
            "app.services.execution_event._get_redis_client", return_value=mock_redis
        ):
            # The actual implementation re-raises the original exception
            with pytest.raises(Exception, match="Redis connection error"):
                await publish_execution_event(event)


@pytest.mark.unit
class TestExecutionEventGracefulShutdown:
    """Tests for graceful shutdown"""

    @pytest.mark.asyncio
    async def test_close_redis_connection(self):
        """Redis 연결 정리 테스트"""
        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()

        with patch("app.services.execution_event._redis_client", mock_redis):
            await close_redis()

            mock_redis.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_redis_when_already_closed(self):
        """이미 정지된 상태에서 close 호출 테스트"""
        mock_redis = None

        with patch("app.services.execution_event._redis_client", mock_redis):
            await close_redis()

    @pytest.mark.asyncio
    async def test_close_redis_with_error(self):
        """Redis 정리 중 에러 처리 테스트"""
        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock(side_effect=Exception("Close error"))

        with patch("app.services.execution_event._redis_client", mock_redis):
            await close_redis()

            mock_redis.close.assert_called_once()
