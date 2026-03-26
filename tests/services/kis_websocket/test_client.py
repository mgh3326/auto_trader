from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.services.kis_websocket import (
    DOMESTIC_EXECUTION_TR_MOCK,
    DOMESTIC_EXECUTION_TR_REAL,
    OVERSEAS_EXECUTION_TR_MOCK,
    OVERSEAS_EXECUTION_TR_REAL,
    KISExecutionWebSocket,
    KISSubscriptionAckError,
)


@pytest.mark.unit
class TestKISWebSocketClient:
    """Tests for KIS WebSocket client (from monolith)"""

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
        assert client.messages_received == 0
        assert client.execution_events_received == 0
        assert client.last_message_at is None
        assert client.last_execution_at is None
        assert client.last_pingpong_at is None

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
                "app.services.kis_websocket_internal.client.websockets.connect",
                new=AsyncMock(return_value=mock_websocket),
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
        called = False

        async def connect_fail_then_success() -> None:
            nonlocal called
            if not called:
                called = True
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
                new=AsyncMock(
                    return_value="ws://ops.koreainvestment.com:21000/tryitout"
                ),
            ),
            patch.object(
                client,
                "_connect_and_subscribe_internal",
                new=AsyncMock(side_effect=connect_fail_then_success),
            ),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch(
                "app.services.kis_websocket_internal.approval_keys._issue_approval_key",
                reissue_mock,
            ),
            patch(
                "app.services.kis_websocket_internal.approval_keys._cache_approval_key",
                cache_mock,
            ),
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
        called = False

        async def connect_fail_then_success() -> None:
            nonlocal called
            if not called:
                called = True
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
                new=AsyncMock(
                    return_value="ws://ops.koreainvestment.com:21000/tryitout"
                ),
            ),
            patch.object(
                client,
                "_connect_and_subscribe_internal",
                new=AsyncMock(side_effect=connect_fail_then_success),
            ),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch(
                "app.services.kis_websocket_internal.approval_keys._issue_approval_key",
                reissue_mock,
            ),
            patch(
                "app.services.kis_websocket_internal.approval_keys._cache_approval_key",
                cache_mock,
            ),
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
                new=AsyncMock(
                    return_value="ws://ops.koreainvestment.com:21000/tryitout"
                ),
            ),
            patch.object(client, "_connect_and_subscribe_internal", connect_mock),
            patch.object(client, "_close_websocket_best_effort", close_mock),
            patch(
                "app.services.kis_websocket_internal.approval_keys._issue_approval_key",
                new=AsyncMock(),
            ),
            patch(
                "app.services.kis_websocket_internal.approval_keys._cache_approval_key",
                new=AsyncMock(),
            ),
        ):
            with pytest.raises(
                RuntimeError, match="KIS WebSocket connection not established"
            ):
                await client.connect_and_subscribe()

        assert connect_mock.await_count == 2
        assert close_mock.await_count == 2

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
            "app.services.kis_websocket_internal.approval_keys.close_approval_key_redis",
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
            "app.services.kis_websocket_internal.approval_keys.close_approval_key_redis",
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
    async def test_listen_logs_domestic_execution_summary_with_correlation_metadata(
        self, execution_callback, caplog
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.websocket = AsyncMock()
        client.websocket.__aiter__.return_value = [
            (
                "0|H0STCNI0|1|"
                "mgh3326^6762259301^0030145286^0000000000^02^0^00^00^012450^2^1135000^093001^N^2^Y^0000^2^홍길동^0^KRX^N^^00^00000000^한화에어로^1135000"
            )
        ]

        with caplog.at_level("INFO"):
            await client.listen()

        execution_callback.assert_awaited_once()
        event = execution_callback.await_args.args[0]
        assert event["symbol"] == "012450"
        assert event["correlation_id"]
        assert event["received_at"]
        assert client.messages_received == 1
        assert client.execution_events_received == 1
        assert client.last_message_at == event["received_at"]
        assert client.last_execution_at == event["received_at"]

    @pytest.mark.asyncio
    async def test_listen_invokes_callback_for_official_overseas_fill(
        self, execution_callback, caplog
    ) -> None:
        """Test that listen() correctly invokes callback for official H0GSCNI0 fill."""
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.websocket = AsyncMock()

        # official H0GSCNI0 message
        payload = (
            "12345678^01^ORD1^0000000000^02^0^153045^NVDA^3^875.00^0000000003^2^0^1"
        )
        message = f"0|H0GSCNI0|1|{payload}"
        client.websocket.__aiter__.return_value = [message]

        with caplog.at_level("INFO"):
            await client.listen()

        execution_callback.assert_awaited_once()
        event = execution_callback.await_args.args[0]
        assert event["symbol"] == "NVDA"
        assert event["execution_status"] == "filled"
        assert client.execution_events_received == 1

    @pytest.mark.asyncio
    async def test_listen_updates_pingpong_state_without_info_log(
        self, execution_callback, caplog
    ):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.websocket = AsyncMock()
        client.websocket.send = AsyncMock()
        client.websocket.__aiter__.return_value = ["0|pingpong"]

        with caplog.at_level("INFO"):
            await client.listen()

        assert client.messages_received == 1
        assert client.execution_events_received == 0
        assert client.last_message_at is not None
        assert client.last_pingpong_at is not None
        execution_callback.assert_not_awaited()
        client.websocket.send.assert_awaited_once_with("0|pingpong")
        assert "KIS pingpong received" not in caplog.text

    def test_get_runtime_snapshot_returns_current_state(self, execution_callback):
        client = KISExecutionWebSocket(on_execution=execution_callback, mock_mode=True)
        client.messages_received = 3
        client.execution_events_received = 2
        client.last_message_at = "2026-03-09T14:05:00+00:00"
        client.last_execution_at = "2026-03-09T14:05:05+00:00"
        client.last_pingpong_at = "2026-03-09T14:05:06+00:00"

        snapshot = client.get_runtime_snapshot()

        assert snapshot == {
            "messages_received": 3,
            "execution_events_received": 2,
            "last_message_at": "2026-03-09T14:05:00+00:00",
            "last_execution_at": "2026-03-09T14:05:05+00:00",
            "last_pingpong_at": "2026-03-09T14:05:06+00:00",
        }

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
            "app.services.kis_websocket_internal.approval_keys.close_approval_key_redis",
            close_redis_mock,
        ):
            await client.stop()

        assert client.is_running is False
        assert client.is_connected is False
        assert client.websocket is None
        close_redis_mock.assert_awaited_once()
