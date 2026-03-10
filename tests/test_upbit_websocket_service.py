"""Unit tests for Upbit MyOrder websocket service."""

import ssl
from unittest.mock import AsyncMock, patch

import pytest

from app.services.upbit_websocket import UpbitMyOrderWebSocket


@pytest.mark.unit
class TestUpbitMyOrderWebSocket:
    """Tests for websocket connection behavior."""

    @pytest.mark.asyncio
    async def test_connect_uses_additional_headers(self):
        """websockets>=15에서 additional_headers를 사용하는지 테스트"""
        client = UpbitMyOrderWebSocket(on_order_callback=AsyncMock())
        mock_websocket = AsyncMock()
        mock_websocket.send = AsyncMock()

        with (
            patch.object(client, "_create_ssl_context", return_value=object()),
            patch.object(client, "_create_auth_token", return_value="token-123"),
            patch(
                "websockets.connect",
                new=AsyncMock(return_value=mock_websocket),
            ) as mock_connect,
            patch.object(
                client, "_listen_for_messages", new=AsyncMock()
            ) as mock_listen,
        ):
            await client._connect_and_subscribe_internal()

        assert mock_connect.await_args is not None
        kwargs = mock_connect.await_args.kwargs
        assert kwargs["additional_headers"] == {
            "Authorization": "Bearer token-123",
        }
        assert "extra_headers" not in kwargs
        mock_listen.assert_awaited_once()
        mock_websocket.send.assert_awaited_once()
        assert client.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_raises_when_auth_token_creation_fails(self):
        """JWT 토큰 생성 실패 시 연결 시도 없이 예외 발생 테스트"""
        client = UpbitMyOrderWebSocket()

        with (
            patch.object(client, "_create_ssl_context", return_value=object()),
            patch.object(client, "_create_auth_token", return_value=None),
            patch("websockets.connect", new=AsyncMock()) as mock_connect,
        ):
            with pytest.raises(Exception, match="JWT 인증 토큰 생성에 실패했습니다."):
                await client._connect_and_subscribe_internal()

        mock_connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_connect_and_subscribe_raises_after_max_attempts(self):
        client = UpbitMyOrderWebSocket()
        client.max_reconnect_attempts = 3
        client.reconnect_delay = 0

        failure = RuntimeError("simulated connection failure")

        with patch.object(
            client,
            "_connect_and_subscribe_internal",
            new=AsyncMock(side_effect=failure),
        ) as connect_mock:
            with pytest.raises(
                RuntimeError,
                match="Upbit WebSocket connection not established",
            ) as exc_info:
                await client.connect_and_subscribe()

        assert connect_mock.await_count == 3
        assert client.current_attempt == client.max_reconnect_attempts
        assert client.is_connected is False
        assert "3/3" in str(exc_info.value)

    def test_create_ssl_context_verifies_by_default(self):
        client = UpbitMyOrderWebSocket()

        ssl_context = client._create_ssl_context()

        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname is True

    def test_create_ssl_context_supports_explicit_insecure_mode(self):
        client = UpbitMyOrderWebSocket(verify_ssl=False)

        ssl_context = client._create_ssl_context()

        assert ssl_context.verify_mode == ssl.CERT_NONE
        assert ssl_context.check_hostname is False
