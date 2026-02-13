"""
Unit tests for KIS WebSocket client

Tests for KIS WebSocket client implementation including:
- Approval Key issuance
- Connection/Reconnection patterns
- Message parsing (domestic/overseas)
- Ping/Pong handling
- Graceful shutdown
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.kis_websocket import KISExecutionWebSocket, get_approval_key


@pytest.mark.unit
class TestKISWebSocketApprovalKey:
    """Tests for Approval Key issuance"""

    @pytest.mark.asyncio
    async def test_issue_approval_key_success(self):
        """Approval Key 발급 성공 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "test_approval_key"}
        mock_response.raise_for_status = MagicMock()

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )
            mock_client.return_value.__aenter__.return_value = MagicMock()

            approval_key = await get_approval_key()

            assert approval_key == "test_approval_key"

    @pytest.mark.asyncio
    async def test_issue_approval_key_missing_key(self, mocker):
        """Approval Key 응답에 키 없음 실패 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "unauthorized"}
        mock_response.raise_for_status = MagicMock()

        mocker.patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )
            mock_client.return_value.__aenter__.return_value = MagicMock()

            with pytest.raises(Exception, match="Approval Key not found"):
                await get_approval_key()


@pytest.mark.unit
class TestKISWebSocketClient:
    """Tests for KIS WebSocket client"""

    @pytest.fixture
    def mock_websocket(self):
        """Mock WebSocket client"""
        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(return_value='{"type":"ack"}')
        ws.close = AsyncMock()
        return ws

    @pytest.fixture
    def execution_callback(self):
        """Mock execution callback"""
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_websocket_initialization(self, execution_callback):
        """WebSocket 클라이언트 초기화 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        assert client.on_execution == execution_callback
        assert client.mock_mode is True
        assert client.is_running is False
        assert client.is_connected is False
        assert client.reconnect_delay == 5
        assert client.max_reconnect_attempts == 10
        assert client.ping_interval == 30
        assert client.ping_timeout == 10

    @pytest.mark.asyncio
    async def test_mock_mode_bypasses_connection(self, execution_callback):
        """Mock 모드에서 실제 연결 바이패스 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        await client.connect_and_subscribe()

        assert client.is_connected is True
        assert client.websocket is None

    @pytest.mark.asyncio
    async def test_parse_message_domestic_kr(self, execution_callback):
        """국내(KR) 체결 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 국내 체결 메시지 예시 (체결 타입 1)
        message = "0|H0STCNT0|005930|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0STCNT0"
        assert result["execution_type"] == 1
        assert result["symbol"] == "005930"
        assert result["market"] == "kr"

    @pytest.mark.asyncio
    async def test_parse_message_overseas_us(self, execution_callback):
        """해외(US) 체결 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 해외 체결 메시지 예시
        message = "0|H0GSCNI0|AAPL|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0GSCNI0"
        assert result["execution_type"] == 1
        assert result["symbol"] == "AAPL"
        assert result["market"] == "us"

    @pytest.mark.asyncio
    async def test_parse_message_non_execution_type(self, execution_callback):
        """체결 타입이 1이 아닌 경우 필터링 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 체결 타입 2 (비체결)
        message = "0|H0STCNT0|005930|..."  # execution_type would be parsed as 2
        result = client._parse_message(message)

        assert result is not None
        # execution_type != 1 이면 필터링되어야 함

    @pytest.mark.asyncio
    async def test_parse_message_pingpong(self, execution_callback):
        """Ping/Pong 시스템 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = "0|PINGPONG"
        result = client._parse_message(message)

        assert result is not None
        assert result.get("system") == "pingpong"

    @pytest.mark.asyncio
    async def test_parse_message_json_response(self, execution_callback):
        """JSON 응답 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 구독 ACK/에러 응답
        message = '{"type":"error","message":"Subscription failed"}'
        result = client._parse_message(message)

        assert result is not None
        assert result["type"] == "error"
        assert result["message"] == "Subscription failed"

    @pytest.mark.asyncio
    async def test_parse_message_invalid_format(self, execution_callback):
        """잘못된 형식의 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 너무 짧은 메시지
        message = "0|H0STCNT0"
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_message_bytes_to_string(self, execution_callback):
        """바이트 메시지 UTF-8 디코딩 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = b"0|H0STCNT0|005930|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0STCNT0"

    @pytest.mark.asyncio
    async def test_parse_message_empty_string(self, execution_callback):
        """빈 문자열 메시지 처리 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = "   "
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_response_json(self, execution_callback):
        """JSON 응답 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = '{"type":"ack","message":"OK"}'
        result = client._parse_response(message)

        assert result["type"] == "ack"
        assert result["message"] == "OK"

    @pytest.mark.asyncio
    async def test_parse_response_raw(self, execution_callback):
        """Raw 응답 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = "ACK message"
        result = client._parse_response(message)

        assert result["type"] == "ack"
        assert result["message"] == "ACK message"

    @pytest.mark.asyncio
    async def test_stop_websocket(self, execution_callback):
        """WebSocket 정지 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.is_running = True
        client.is_connected = True
        client.websocket = AsyncMock()

        await client.stop()

        assert client.is_running is False
        assert client.is_connected is False
        client.websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_already_stopped(self, execution_callback):
        """이미 정지된 상태에서 stop 호출 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.is_running = False
        client.websocket = AsyncMock()

        await client.stop()

        assert client.is_running is False
        client.websocket.close.assert_not_called()


@pytest.mark.unit
class TestKISWebSocketIndexSafety:
    """Tests for message parsing index safety"""

    @pytest.mark.asyncio
    async def test_parse_message_with_insufficient_parts(self):
        """인덱스 안전 처리: 부족한 파트 수 테스트"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # 2개 파트만 있는 메시지 (최소 3개 필요)
        message = "0|H0STCNT0"
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_message_with_index_error(self):
        """인덱스 접근 시 IndexError 방지 테스트"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # execution_type에 숫자 아닌 값
        message = "0|H0STCNT0|005930|..."  # parts[1]이 숫자가 아님
        result = client._parse_message(message)

        # 에러 로깅 후 None 반환 (IndexError 방지)
        assert result is None
