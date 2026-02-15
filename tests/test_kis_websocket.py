"""
Unit tests for KIS WebSocket client

Tests for KIS WebSocket client implementation including:
- Approval Key issuance
- Connection/Reconnection patterns
- Message parsing (domestic/overseas)
- Ping/Pong handling
- Graceful shutdown
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.config import settings
from app.services.kis_websocket import (
    APPROVAL_KEY_CACHE_KEY,
    APPROVAL_KEY_TTL_SECONDS,
    DOMESTIC_EXECUTION_TR_MOCK,
    DOMESTIC_EXECUTION_TR_REAL,
    KISSubscriptionAckError,
    KISExecutionWebSocket,
    OVERSEAS_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_MOCK,
    _cache_approval_key,
    _get_cached_approval_key,
    _is_valid_approval_key,
    close_approval_key_redis,
    get_approval_key,
)


@pytest.mark.unit
class TestKISWebSocketApprovalKey:
    """Tests for Approval Key issuance"""

    @pytest.mark.asyncio
    async def test_issue_approval_key_success(self):
        """Approval Key 발급 성공 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "test_approval_key"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "app.services.kis_websocket._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    return_value=None,
                ):
                    approval_key = await get_approval_key()

                    assert approval_key == "test_approval_key"

    @pytest.mark.asyncio
    async def test_issue_approval_key_missing_key(self):
        """Approval Key 응답에 키 없음 실패 케이스"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "unauthorized"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "app.services.kis_websocket._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    return_value=None,
                ):
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
    async def test_build_websocket_url_real_and_mock(self, execution_callback):
        real_client = KISExecutionWebSocket(
            on_execution=execution_callback, mock_mode=False
        )
        mock_client = KISExecutionWebSocket(
            on_execution=execution_callback, mock_mode=True
        )

        assert (
            await real_client._build_websocket_url()
            == "ws://ops.koreainvestment.com:21000/tryitout"
        )
        assert (
            await mock_client._build_websocket_url()
            == "ws://ops.koreainvestment.com:31000/tryitout"
        )

    @pytest.mark.asyncio
    async def test_connect_internal_omits_ssl_for_ws_scheme(self, execution_callback):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.websocket_url = "ws://ops.koreainvestment.com:21000/tryitout"

        mock_websocket = AsyncMock()

        with (
            patch(
                "websockets.connect", new=AsyncMock(return_value=mock_websocket)
            ) as mock_connect,
            patch.object(client, "_subscribe_execution_tr", new=AsyncMock()),
        ):
            await client._connect_and_subscribe_internal()

        assert mock_connect.await_count == 1
        kwargs = mock_connect.await_args_list[0][1]
        assert "ssl" not in kwargs

    @pytest.mark.asyncio
    async def test_build_subscription_request_json_structure(self, execution_callback):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.approval_key = "approval-key"

        request = client._build_subscription_request("H0STCNI0", "hts-user")

        assert request["header"]["approval_key"] == "approval-key"
        assert request["header"]["custtype"] == "P"
        assert request["header"]["tr_type"] == "1"
        assert request["header"]["content-type"] == "utf-8"
        assert request["body"]["input"]["tr_id"] == "H0STCNI0"
        assert request["body"]["input"]["tr_key"] == "hts-user"

    @pytest.mark.asyncio
    async def test_subscribe_execution_tr_uses_mock_tr_ids(self, execution_callback):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.approval_key = "approval-key"

        send_mock = AsyncMock()
        with (
            patch.object(client, "_send_subscription_request", send_mock),
            patch.object(settings, "kis_ws_hts_id", "hts-user"),
        ):
            await client._subscribe_execution_tr()

        assert send_mock.await_count == 2
        first_request, first_tr = send_mock.await_args_list[0][0]
        second_request, second_tr = send_mock.await_args_list[1][0]

        assert first_tr == DOMESTIC_EXECUTION_TR_MOCK
        assert second_tr == OVERSEAS_EXECUTION_TR_MOCK
        assert first_request["body"]["input"]["tr_id"] == DOMESTIC_EXECUTION_TR_MOCK
        assert second_request["body"]["input"]["tr_id"] == OVERSEAS_EXECUTION_TR_MOCK
        assert first_request["body"]["input"]["tr_key"] == "hts-user"
        assert second_request["body"]["input"]["tr_key"] == "hts-user"

    @pytest.mark.asyncio
    async def test_validate_subscription_ack_stores_key_and_iv(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)

        parsed = {
            "header": {"tr_id": DOMESTIC_EXECUTION_TR_REAL},
            "body": {
                "rt_cd": "0",
                "msg_cd": "OPSP0000",
                "msg1": "OK",
                "output": {"key": "test-key-123456", "iv": "1234567890ABCDEF"},
            },
        }

        client._validate_subscription_ack(
            parsed, expected_tr_id=DOMESTIC_EXECUTION_TR_REAL
        )

        assert client._encryption_keys_by_tr[DOMESTIC_EXECUTION_TR_REAL] == (
            "test-key-123456",
            "1234567890ABCDEF",
        )

    @pytest.mark.asyncio
    async def test_validate_subscription_ack_fails_when_rt_cd_not_zero(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)

        parsed = {
            "header": {"tr_id": DOMESTIC_EXECUTION_TR_REAL},
            "body": {
                "rt_cd": "1",
                "msg_cd": "ERROR",
                "msg1": "failure",
            },
        }

        with pytest.raises(KISSubscriptionAckError, match="Subscription failed"):
            client._validate_subscription_ack(
                parsed, expected_tr_id=DOMESTIC_EXECUTION_TR_REAL
            )

    @pytest.mark.asyncio
    async def test_reissues_approval_key_on_invalid_approval_msg_code(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.is_running = True
        client.reconnect_delay = 0
        client.max_reconnect_attempts = 3
        client.approval_key = "cached-key"

        async def connect_fail_then_success() -> None:
            if not hasattr(connect_fail_then_success, "called"):
                connect_fail_then_success.called = True
                raise KISSubscriptionAckError(
                    tr_id=DOMESTIC_EXECUTION_TR_REAL,
                    rt_cd="1",
                    msg_cd="OPSP0011",
                    msg1="invalid approval : NOT FOUND",
                )
            client.is_connected = True

        reissue_mock = AsyncMock(return_value="fresh-key")
        cache_mock = AsyncMock()
        close_mock = AsyncMock()

        with (
            patch.object(
                client,
                "_issue_approval_key_if_needed",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                client,
                "_build_websocket_url",
                new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
            ),
            patch.object(
                client,
                "_connect_and_subscribe_internal",
                new=AsyncMock(side_effect=connect_fail_then_success),
            ),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch("app.services.kis_websocket._issue_approval_key", reissue_mock),
            patch("app.services.kis_websocket._cache_approval_key", cache_mock),
        ):
            await client.connect_and_subscribe()

        assert client.is_connected is True
        assert client.approval_key == "fresh-key"
        reissue_mock.assert_awaited_once()
        cache_mock.assert_awaited_once_with("fresh-key")
        assert close_mock.await_count >= 1

    @pytest.mark.asyncio
    async def test_reissues_approval_key_on_already_in_use_msg_code(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.is_running = True
        client.reconnect_delay = 0
        client.max_reconnect_attempts = 3
        client.approval_key = "cached-key"

        async def connect_fail_then_success() -> None:
            if not hasattr(connect_fail_then_success, "called"):
                connect_fail_then_success.called = True
                raise KISSubscriptionAckError(
                    tr_id=DOMESTIC_EXECUTION_TR_REAL,
                    rt_cd="9",
                    msg_cd="OPSP8996",
                    msg1="ALREADY IN USE appkey",
                )
            client.is_connected = True

        reissue_mock = AsyncMock(return_value="fresh-key-2")
        cache_mock = AsyncMock()
        close_mock = AsyncMock()

        with (
            patch.object(
                client,
                "_issue_approval_key_if_needed",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                client,
                "_build_websocket_url",
                new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
            ),
            patch.object(
                client,
                "_connect_and_subscribe_internal",
                new=AsyncMock(side_effect=connect_fail_then_success),
            ),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch("app.services.kis_websocket._issue_approval_key", reissue_mock),
            patch("app.services.kis_websocket._cache_approval_key", cache_mock),
        ):
            await client.connect_and_subscribe()

        assert client.is_connected is True
        assert client.approval_key == "fresh-key-2"
        reissue_mock.assert_awaited_once()
        cache_mock.assert_awaited_once_with("fresh-key-2")
        assert close_mock.await_count >= 1

    @pytest.mark.asyncio
    async def test_connect_and_subscribe_raises_after_max_attempts(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.is_running = True
        client.reconnect_delay = 0
        client.max_reconnect_attempts = 2
        client.approval_key = "cached-key"

        connect_mock = AsyncMock(
            side_effect=KISSubscriptionAckError(
                tr_id=OVERSEAS_EXECUTION_TR_REAL,
                rt_cd="9",
                msg_cd="OPSP0001",
                msg1="fatal ack error",
            )
        )
        close_mock = AsyncMock()

        with (
            patch.object(
                client,
                "_issue_approval_key_if_needed",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                client,
                "_build_websocket_url",
                new=AsyncMock(return_value="ws://ops.koreainvestment.com:21000/tryitout"),
            ),
            patch.object(client, "_connect_and_subscribe_internal", connect_mock),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch("app.services.kis_websocket._issue_approval_key", new=AsyncMock()),
            patch("app.services.kis_websocket._cache_approval_key", new=AsyncMock()),
        ):
            with pytest.raises(
                RuntimeError, match="KIS WebSocket connection not established"
            ):
                await client.connect_and_subscribe()

        assert connect_mock.await_count == 2
        assert close_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_parse_message_decrypts_encrypted_frame(self, execution_callback):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        key = "0123456789ABCDEF0123456789ABCDEF"
        iv = "1234567890ABCDEF"
        client._encryption_keys_by_tr[DOMESTIC_EXECUTION_TR_REAL] = (key, iv)

        plain_payload = "005930^02^A123456789^70000^10^093001"
        padder = padding.PKCS7(algorithms.AES.block_size).padder()
        padded_payload = (
            padder.update(plain_payload.encode("utf-8")) + padder.finalize()
        )
        encryptor = Cipher(
            algorithms.AES(key.encode("utf-8")),
            modes.CBC(iv.encode("utf-8")),
        ).encryptor()
        encrypted_payload = encryptor.update(padded_payload) + encryptor.finalize()
        encrypted_base64 = base64.b64encode(encrypted_payload).decode("utf-8")

        message = f"1|{DOMESTIC_EXECUTION_TR_REAL}|005930|{encrypted_base64}"
        result = client._parse_message(message)

        assert result is not None
        assert result["symbol"] == "005930"
        assert result["side"] == "bid"
        assert result["filled_price"] == 70000
        assert result["filled_qty"] == 10

    @pytest.mark.asyncio
    async def test_parse_message_returns_none_when_encryption_key_missing(
        self, execution_callback
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)

        message = f"1|{DOMESTIC_EXECUTION_TR_REAL}|005930|YWJj"
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_message_domestic_kr(self, execution_callback):
        """국내(KR) 체결 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 국내 체결 메시지: tr_code|execution_type|symbol|...
        message = "H0STCNI0|1|005930|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0STCNI0"
        assert result["execution_type"] == 1
        assert result["symbol"] == "005930"
        assert result["market"] == "kr"

    @pytest.mark.asyncio
    async def test_parse_message_overseas_us(self, execution_callback):
        """해외(US) 체결 메시지 파싱 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 해외 체결 메시지: tr_code|execution_type|symbol|...
        message = "H0GSCNI0|1|AAPL|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0GSCNI0"
        assert result["execution_type"] == 1
        assert result["symbol"] == "AAPL"
        assert result["market"] == "us"

    @pytest.mark.asyncio
    async def test_parse_message_non_execution_type(self, execution_callback):
        """0|TR코드 envelope 형식은 execution_type=1로 정규화"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        # 0|TR|count|payload 형식
        message = "0|H0STCNI0|005930|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["execution_type"] == 1
        assert result["tr_code"] == "H0STCNI0"
        assert result["market"] == "kr"

    @pytest.mark.asyncio
    async def test_parse_message_pingpong(self, execution_callback):
        """Ping/Pong 시스템 메시지는 분기 처리 가능한 형태로 파싱"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = "0|pingpong"
        result = client._parse_message(message)

        assert result is not None
        assert result["system"] == "pingpong"

    @pytest.mark.asyncio
    async def test_parse_message_extracts_fill_fields_best_effort(
        self, execution_callback
    ):
        """payload에서 side/price/qty/order_id/timestamp를 best-effort로 추출"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)

        message = "0|H0STCNI0|1|005930^02^A123456789^70000^10^093001"
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0STCNI0"
        assert result["symbol"] == "005930"
        assert result["side"] == "bid"
        assert result["order_id"] == "A123456789"
        assert result["filled_price"] == 70000
        assert result["filled_qty"] == 10
        assert "filled_at" in result

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

        message = b"H0STCNI0|1|005930|..."
        result = client._parse_message(message)

        assert result is not None
        assert result["tr_code"] == "H0STCNI0"
        assert result["symbol"] == "005930"
        assert result["market"] == "kr"

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
        ws_mock = client.websocket

        close_redis_mock = AsyncMock()
        with patch(
            "app.services.kis_websocket.close_approval_key_redis",
            close_redis_mock,
        ):
            await client.stop()

        assert client.is_running is False
        assert client.is_connected is False
        assert client.websocket is None
        ws_mock.close.assert_awaited_once()
        close_redis_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_already_stopped(self, execution_callback):
        """이미 정지된 상태에서도 cleanup 수행 테스트"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.is_running = False
        client.websocket = AsyncMock()

        ws_mock = client.websocket
        close_redis_mock = AsyncMock()
        with patch(
            "app.services.kis_websocket.close_approval_key_redis",
            close_redis_mock,
        ):
            await client.stop()

        assert client.is_running is False
        assert client.is_connected is False
        assert client.websocket is None
        ws_mock.close.assert_awaited_once()
        close_redis_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_subscription_request_without_websocket_raises(
        self, execution_callback
    ):
        """웹소켓 미초기화 상태에서 구독 요청 시 RuntimeError"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.websocket = None

        with pytest.raises(RuntimeError, match="WebSocket is not connected"):
            await client._send_subscription_request(
                {
                    "header": {},
                    "body": {"input": {"tr_id": DOMESTIC_EXECUTION_TR_REAL}},
                },
                DOMESTIC_EXECUTION_TR_REAL,
            )

    @pytest.mark.asyncio
    async def test_listen_without_websocket_raises(self, execution_callback):
        """웹소켓 미초기화 상태에서 listen 호출 시 RuntimeError"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=False)
        client.websocket = None

        with pytest.raises(RuntimeError, match="WebSocket is not connected"):
            await client.listen()

    @pytest.mark.asyncio
    async def test_stop_still_closes_redis_when_websocket_close_fails(
        self, execution_callback
    ):
        """웹소켓 close 실패 시에도 Redis cleanup 수행"""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.is_running = True
        client.is_connected = True
        client.websocket = AsyncMock()
        client.websocket.close = AsyncMock(side_effect=RuntimeError("close failed"))

        close_redis_mock = AsyncMock()
        with patch(
            "app.services.kis_websocket.close_approval_key_redis",
            close_redis_mock,
        ):
            await client.stop()

        assert client.is_running is False
        assert client.is_connected is False
        assert client.websocket is None
        close_redis_mock.assert_awaited_once()


@pytest.mark.unit
class TestKISWebSocketIndexSafety:
    """Tests for message parsing index safety"""

    @pytest.mark.asyncio
    async def test_parse_message_with_insufficient_parts(self):
        """인덱스 안전 처리: 부족한 파트 수 테스트"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # 2개 파트만 있는 메시지 (최소 3개 필요)
        message = "0|H0STCNI0"
        result = client._parse_message(message)

        assert result is None

    @pytest.mark.asyncio
    async def test_parse_message_with_non_digit_execution_type(self):
        """execution_type이 숫자가 아닌 경우 None 반환하지 않고 dict 반환"""
        client = KISExecutionWebSocket(on_execution=AsyncMock(), mock_mode=True)

        # parts[1]이 숫자가 아님 -> execution_type은 None이지만 dict는 반환됨
        message = "0|abc|005930|..."
        result = client._parse_message(message)

        # dict는 반환되지만 execution_type은 None
        assert result is not None
        assert result["execution_type"] is None


@pytest.mark.unit
class TestApprovalKeyRedisCache:
    """Tests for Approval Key Redis caching"""

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_hit(self):
        """Redis GET 성공 시 캐시된 키 반환"""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="cached_approval_key_123")

        with patch(
            "app.services.kis_websocket._get_redis_client",
            return_value=mock_redis,
        ):
            result = await _get_cached_approval_key()

            assert result == "cached_approval_key_123"
            mock_redis.get.assert_called_once_with(APPROVAL_KEY_CACHE_KEY)

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_miss(self):
        """Redis GET 빈값(None) 시 None 반환"""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)

        with patch(
            "app.services.kis_websocket._get_redis_client",
            return_value=mock_redis,
        ):
            result = await _get_cached_approval_key()

            assert result is None
            mock_redis.get.assert_called_once_with(APPROVAL_KEY_CACHE_KEY)

    @pytest.mark.asyncio
    async def test_get_cached_approval_key_redis_error_propagates(self):
        """Redis 예외 발생 시 전파 (엄격 실패 정책)"""
        from redis.asyncio import RedisError

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=RedisError("Connection refused"))

        with patch(
            "app.services.kis_websocket._get_redis_client",
            return_value=mock_redis,
        ):
            with pytest.raises(RedisError, match="Connection refused"):
                await _get_cached_approval_key()

    @pytest.mark.asyncio
    async def test_cache_approval_key_sets_with_ttl(self):
        """Redis SET 호출 시 23시간 TTL 적용"""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch(
            "app.services.kis_websocket._get_redis_client",
            return_value=mock_redis,
        ):
            await _cache_approval_key("new_approval_key_456")

            mock_redis.set.assert_called_once_with(
                APPROVAL_KEY_CACHE_KEY,
                "new_approval_key_456",
                ex=APPROVAL_KEY_TTL_SECONDS,
            )

    @pytest.mark.asyncio
    async def test_cache_approval_key_redis_error_propagates(self):
        """Redis SET 예외 발생 시 전파 (엄격 실패 정책)"""
        from redis.asyncio import RedisError

        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(side_effect=RedisError("Write failed"))

        with patch(
            "app.services.kis_websocket._get_redis_client",
            return_value=mock_redis,
        ):
            with pytest.raises(RedisError, match="Write failed"):
                await _cache_approval_key("new_key")

    @pytest.mark.asyncio
    async def test_get_approval_key_uses_cached_value(self):
        """캐시 히트 시 재발급 없이 캐시 값 반환"""
        with patch(
            "app.services.kis_websocket._get_cached_approval_key",
            return_value="cached_key_789",
        ):
            result = await get_approval_key()

            assert result == "cached_key_789"

    @pytest.mark.asyncio
    async def test_get_approval_key_issues_and_caches_on_miss(self):
        """캐시 미스 시 새로 발급하고 캐시에 저장"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_abc"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "app.services.kis_websocket._get_cached_approval_key",
                return_value=None,
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    cache_spy,
                ):
                    result = await get_approval_key()

                    assert result == "fresh_key_abc"
                    cache_spy.assert_called_once_with("fresh_key_abc")

    @pytest.mark.asyncio
    async def test_cache_constants_are_correct(self):
        """캐시 상수값 검증"""
        assert APPROVAL_KEY_CACHE_KEY == "kis:websocket:approval_key"
        assert APPROVAL_KEY_TTL_SECONDS == 82800  # 23시간


@pytest.mark.unit
class TestApprovalKeyValidation:
    """Tests for Approval Key validation helper"""

    def test_valid_key_returns_true(self):
        """유효한 키는 True 반환"""
        assert _is_valid_approval_key("valid_key_123") is True

    def test_none_returns_false(self):
        """None은 False 반환"""
        assert _is_valid_approval_key(None) is False

    def test_empty_string_returns_false(self):
        """빈 문자열은 False 반환"""
        assert _is_valid_approval_key("") is False

    def test_whitespace_only_returns_false(self):
        """공백만 있는 문자열은 False 반환"""
        assert _is_valid_approval_key("   ") is False
        assert _is_valid_approval_key("\t\n") is False

    def test_key_with_surrounding_whitespace_is_valid(self):
        """앞뒤 공백이 있는 키는 유효"""
        assert _is_valid_approval_key("  valid_key  ") is True


@pytest.mark.unit
class TestApprovalKeyEmptyCacheMiss:
    """Tests for empty/whitespace cache values being treated as cache miss"""

    @pytest.mark.asyncio
    async def test_empty_string_cache_treated_as_miss(self):
        """빈 문자열 캐시값은 미스로 처리되어 재발급"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_empty"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "app.services.kis_websocket._get_cached_approval_key",
                return_value="",  # Empty string from cache
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    cache_spy,
                ):
                    result = await get_approval_key()

                    assert result == "fresh_key_empty"
                    cache_spy.assert_called_once_with("fresh_key_empty")

    @pytest.mark.asyncio
    async def test_whitespace_cache_treated_as_miss(self):
        """공백 캐시값은 미스로 처리되어 재발급"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"approval_key": "fresh_key_ws"}
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.post = AsyncMock(return_value=mock_response)

        cache_spy = AsyncMock()

        with patch("app.services.kis_websocket.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "app.services.kis_websocket._get_cached_approval_key",
                return_value="   ",  # Whitespace from cache
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    cache_spy,
                ):
                    result = await get_approval_key()

                    assert result == "fresh_key_ws"
                    cache_spy.assert_called_once_with("fresh_key_ws")


@pytest.mark.unit
class TestApprovalKeyCacheHitNoReissue:
    """Tests for cache hit blocking re-issuance"""

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_call_issue_or_cache(self):
        """캐시 히트 시 _issue_approval_key와 _cache_approval_key 호출되지 않음"""
        issue_spy = AsyncMock(return_value="should_not_be_called")
        cache_spy = AsyncMock()

        with patch(
            "app.services.kis_websocket._get_cached_approval_key",
            return_value="cached_valid_key",
        ):
            with patch(
                "app.services.kis_websocket._issue_approval_key",
                issue_spy,
            ):
                with patch(
                    "app.services.kis_websocket._cache_approval_key",
                    cache_spy,
                ):
                    result = await get_approval_key()

                    assert result == "cached_valid_key"
                    issue_spy.assert_not_called()
                    cache_spy.assert_not_called()


@pytest.mark.unit
class TestCloseApprovalKeyRedis:
    """Tests for Redis client cleanup function"""

    @pytest.mark.asyncio
    async def test_close_existing_client(self):
        """기존 클라이언트 존재 시 close 호출"""
        import app.services.kis_websocket as mod

        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()
        mod._redis_client = mock_redis

        await close_approval_key_redis()

        mock_redis.close.assert_called_once()
        assert mod._redis_client is None

    @pytest.mark.asyncio
    async def test_close_no_client_is_idempotent(self):
        """클라이언트 없을 때 호출해도 예외 없음 (idempotent)"""
        import app.services.kis_websocket as mod

        mod._redis_client = None

        # Should not raise
        await close_approval_key_redis()

        assert mod._redis_client is None

    @pytest.mark.asyncio
    async def test_close_multiple_times_is_idempotent(self):
        """여러 번 호출해도 안전 (idempotent)"""
        import app.services.kis_websocket as mod

        mock_redis = AsyncMock()
        mock_redis.close = AsyncMock()
        mod._redis_client = mock_redis

        await close_approval_key_redis()
        assert mock_redis.close.call_count == 1

        # Second call should be safe
        await close_approval_key_redis()
        assert mock_redis.close.call_count == 1  # Not called again

        assert mod._redis_client is None
