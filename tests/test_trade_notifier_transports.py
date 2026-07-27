# tests/test_trade_notifier_transports.py
"""Tests for trade notifier transport functions."""

import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.monitoring.trade_notifier.transports import (
    send_discord_content_single,
    send_discord_embed_single,
    send_telegram,
)
from app.telegram_contract import TELEGRAM_SEND_MESSAGE_TEXT_LIMIT, telegram_text_length


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

    async def test_returns_false_if_only_some_chats_succeed(self, mock_http_client):
        """A configured mirror is incomplete unless every chat receives it."""
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
        assert result is False

    async def test_plain_text_avoids_triage_markdown_parse_failure(self):
        triage = "\n".join(
            [
                "[watch triage] 214150",
                "market: kr",
                "## 알림 요약",
                "- price_breakout_* 후보 [원문](https://example.invalid)",
                "## 제안 verdict",
                "- verdict=`wait` because risk_score_under_limit",
            ]
        )

        async def telegram_api(_url, *, json):
            request = httpx.Request("POST", "https://api.telegram.org/sendMessage")
            if json.get("parse_mode") == "Markdown":
                return httpx.Response(
                    400,
                    request=request,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": (
                            "Bad Request: can't parse entities: "
                            "Can't find end of the entity starting at byte offset 91"
                        ),
                    },
                )
            return httpx.Response(200, request=request, json={"ok": True})

        client = MagicMock()
        client.post = AsyncMock(side_effect=telegram_api)

        markdown_result = await send_telegram(
            http_client=client,
            bot_token="request-token",
            chat_ids=["998877"],
            text=triage,
            parse_mode="Markdown",
        )
        plain_result = await send_telegram(
            http_client=client,
            bot_token="request-token",
            chat_ids=["998877"],
            text=triage,
            parse_mode=None,
        )

        assert markdown_result is False
        assert plain_result is True
        assert "parse_mode" not in client.post.await_args.kwargs["json"]

    async def test_splits_long_plain_triage_without_losing_text(self):
        triage = "\n".join(
            [
                "[watch triage] 214150",
                "## 알림 요약",
                ("risk_score_under_limit * [source] `wait` _ " * 140),
                "## 제안 verdict",
                ("- verdict=`wait`; 신규 주문 없음\n" * 80),
            ]
        )
        assert telegram_text_length(triage) > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
        response = MagicMock()
        response.raise_for_status = MagicMock()
        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        result = await send_telegram(
            http_client=client,
            bot_token="request-token",
            chat_ids=["998877"],
            text=triage,
            parse_mode=None,
        )

        assert result is True
        payloads = [call.kwargs["json"] for call in client.post.await_args_list]
        assert len(payloads) > 1
        assert "".join(payload["text"] for payload in payloads) == triage
        assert all(
            telegram_text_length(payload["text"]) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT
            for payload in payloads
        )
        assert all("parse_mode" not in payload for payload in payloads)

    async def test_retries_rate_limit_with_a_hard_attempt_cap(self, monkeypatch):
        request = httpx.Request("POST", "https://api.telegram.org/sendMessage")
        rate_limited = httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "0"},
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 0",
            },
        )
        success = httpx.Response(200, request=request, json={"ok": True})
        client = MagicMock()
        client.post = AsyncMock(
            side_effect=[rate_limited, rate_limited, rate_limited, success]
        )
        sleep = AsyncMock()
        monkeypatch.setattr(
            "app.monitoring.trade_notifier.transports.asyncio.sleep", sleep
        )

        result = await send_telegram(
            http_client=client,
            bot_token="request-token",
            chat_ids=["998877"],
            text="triage",
            parse_mode=None,
        )

        assert result is False
        assert client.post.await_count == 3
        assert sleep.await_count == 2

    async def test_retries_network_errors_then_succeeds_without_secret_logs(
        self, monkeypatch, caplog
    ):
        token = "123456:top-secret"
        chat_id = "998877"
        request = httpx.Request(
            "POST", f"https://api.telegram.org/bot{token}/sendMessage"
        )
        client = MagicMock()
        client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError(
                    f"network down for chat={chat_id} via {request.url}",
                    request=request,
                ),
                MagicMock(raise_for_status=MagicMock()),
            ]
        )
        sleep = AsyncMock()
        monkeypatch.setattr(
            "app.monitoring.trade_notifier.transports.asyncio.sleep", sleep
        )

        with caplog.at_level(logging.WARNING):
            result = await send_telegram(
                http_client=client,
                bot_token=token,
                chat_ids=[chat_id],
                text="triage",
                parse_mode=None,
            )

        assert result is True
        assert client.post.await_count == 2
        sleep.assert_awaited_once()
        assert token not in caplog.text
        assert chat_id not in caplog.text

    async def test_logs_safe_response_reason_and_chat_fingerprint(self, caplog):
        request = httpx.Request("POST", "https://api.telegram.org/sendMessage")
        response = httpx.Response(
            400,
            request=request,
            json={
                "ok": False,
                "error_code": 400,
                "description": (
                    "Bad Request: can't parse entities: "
                    "Can't find end of the entity starting at byte offset 91"
                ),
            },
        )
        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        with caplog.at_level(logging.ERROR):
            result = await send_telegram(
                http_client=client,
                bot_token="123456:top-secret",
                chat_ids=["998877"],
                text="triage",
            )

        assert result is False
        record = next(
            item
            for item in caplog.records
            if item.msg.startswith("telegram.send.failed")
        )
        assert record.exception_type == "HTTPStatusError"
        assert "400 Bad Request" in record.exception_message
        assert record.response_body == {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities (byte_offset=91)",
        }
        assert record.chat_ref.startswith("sha256:")
        assert "can't parse entities" in caplog.text
        assert "response_body=" in caplog.text
        assert "123456:top-secret" not in caplog.text
        assert "998877" not in caplog.text


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
