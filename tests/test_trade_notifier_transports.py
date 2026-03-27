# tests/test_trade_notifier_transports.py
"""Tests for trade notifier transport functions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.monitoring.trade_notifier.transports import (
    send_discord_content_single,
    send_discord_embed_single,
    send_telegram,
)


@pytest.fixture
def mock_http_client():
    client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post.return_value = response
    return client


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendTelegram:
    async def test_sends_to_all_chat_ids(self, mock_http_client):
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="test_token",
            chat_ids=["111", "222"],
            text="hello",
        )
        assert result is True
        assert mock_http_client.post.call_count == 2

    async def test_returns_false_when_all_fail(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("network error")
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="test_token",
            chat_ids=["111"],
            text="hello",
        )
        assert result is False

    async def test_sends_correct_payload(self, mock_http_client):
        await send_telegram(
            http_client=mock_http_client,
            bot_token="tok123",
            chat_ids=["999"],
            text="msg",
            parse_mode="Markdown",
        )
        call_kwargs = mock_http_client.post.call_args
        assert "tok123" in call_kwargs.args[0]
        payload = call_kwargs.kwargs["json"]
        assert payload["chat_id"] == "999"
        assert payload["text"] == "msg"
        assert payload["parse_mode"] == "Markdown"

    async def test_returns_true_if_at_least_one_succeeds(self, mock_http_client):
        """Partial success: first chat fails, second succeeds."""
        mock_http_client.post.side_effect = [
            Exception("fail"),
            MagicMock(raise_for_status=MagicMock()),
        ]
        result = await send_telegram(
            http_client=mock_http_client,
            bot_token="tok",
            chat_ids=["a", "b"],
            text="msg",
        )
        assert result is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendDiscordEmbedSingle:
    async def test_sends_embed_payload(self, mock_http_client):
        embed = {
            "title": "test",
            "description": "desc",
            "color": 0x00FF00,
            "fields": [],
        }
        result = await send_discord_embed_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/api/webhooks/123",
            embed=embed,
        )
        assert result is True
        call_kwargs = mock_http_client.post.call_args
        assert call_kwargs.args[0] == "https://discord.com/api/webhooks/123"
        assert call_kwargs.kwargs["json"]["embeds"] == [embed]

    async def test_returns_false_on_failure(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("err")
        result = await send_discord_embed_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/x",
            embed={"title": "t", "description": "d", "color": 0, "fields": []},
        )
        assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
class TestSendDiscordContentSingle:
    async def test_sends_content_payload(self, mock_http_client):
        result = await send_discord_content_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/api/webhooks/456",
            content="hello",
        )
        assert result is True
        call_kwargs = mock_http_client.post.call_args
        assert call_kwargs.kwargs["json"]["content"] == "hello"

    async def test_returns_false_on_failure(self, mock_http_client):
        mock_http_client.post.side_effect = Exception("err")
        result = await send_discord_content_single(
            http_client=mock_http_client,
            webhook_url="https://discord.com/x",
            content="fail",
        )
        assert result is False
