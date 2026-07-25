"""Tests for Telegram inline-keyboard transport and notifier helpers."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.monitoring.trade_notifier import TradeNotifier, transports
from app.telegram_contract import TelegramErrorClassification


def _telegram_response(*, message_id: int = 42) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(
        return_value={"ok": True, "result": {"message_id": message_id}}
    )
    return response


@pytest.fixture
def notifier() -> TradeNotifier:
    TradeNotifier._instance = None
    TradeNotifier._initialized = False
    instance = TradeNotifier()
    yield instance
    TradeNotifier._instance = None
    TradeNotifier._initialized = False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_telegram_without_markup_preserves_payload() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response(message_id=5))

    result = await transports.send_telegram(
        http_client=client,
        bot_token="T",
        chat_ids=["1"],
        text="hi",
    )

    assert result is True
    _, kwargs = client.post.call_args
    assert kwargs["json"] == {
        "chat_id": "1",
        "text": "hi",
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_telegram_forwards_reply_markup() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response())
    keyboard = {"inline_keyboard": [[{"text": "승인", "callback_data": "op:x:y"}]]}

    result = await transports.send_telegram(
        http_client=client,
        bot_token="T",
        chat_ids=["1"],
        text="approve?",
        reply_markup=keyboard,
    )

    assert result is True
    _, kwargs = client.post.call_args
    assert kwargs["json"]["reply_markup"] == keyboard


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_telegram_message_returns_message_id_with_markup() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response(message_id=42))
    keyboard = {"inline_keyboard": [[{"text": "승인", "callback_data": "op:x:y"}]]}

    result = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="approve?",
        reply_markup=keyboard,
    )

    assert result.ok is True
    assert result.message_id == 42
    assert result.status_code == 200
    assert result.payload_chars == len("approve?")
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.telegram.org/botT/sendMessage"
    assert kwargs["json"] == {
        "chat_id": "1",
        "text": "approve?",
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": keyboard,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_telegram_message_returns_structured_transport_failure() -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("network error"))

    result = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="approve?",
    )

    assert result.ok is False
    assert result.message_id is None
    assert result.status_code is None
    assert result.failure_code == "telegram_transport_error"
    assert result.error_classification is TelegramErrorClassification.TRANSPORT_ERROR
    assert not hasattr(result, "description")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_exception_log_omits_all_sensitive_payload_fields(
    caplog,
) -> None:
    bot_token = "123456:secret-bot-token"
    callback_data = "op:deadbeef:secret-nonce"
    thesis_marker = "SECRET-THESIS-CONTENT"
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=RuntimeError(f"https://api.telegram.org/bot{bot_token}/sendMessage")
    )

    with caplog.at_level(logging.ERROR):
        result = await transports.send_telegram_message(
            http_client=client,
            bot_token=bot_token,
            chat_id="1",
            text=thesis_marker,
            reply_markup={
                "inline_keyboard": [[{"text": "승인", "callback_data": callback_data}]]
            },
        )

    assert result.ok is False
    assert bot_token not in caplog.text
    assert callback_data not in caplog.text
    assert "secret-nonce" not in caplog.text
    assert thesis_marker not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("length", "expected_ok", "expected_post_count"),
    [(4095, True, 1), (4096, True, 1), (4097, False, 0)],
)
async def test_send_telegram_message_text_limit_boundaries(
    length: int, expected_ok: bool, expected_post_count: int
) -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response(message_id=91))

    result = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="x" * length,
    )

    assert result.ok is expected_ok
    assert result.payload_chars == length
    assert client.post.await_count == expected_post_count
    if expected_ok:
        assert result.message_id == 91
    else:
        assert result.failure_code == "telegram_payload_too_long"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("emoji_count", "expected_units", "expected_ok"),
    [(2048, 4096, True), (2049, 4098, False)],
)
async def test_send_telegram_message_uses_conservative_utf16_units(
    emoji_count: int, expected_units: int, expected_ok: bool
) -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response(message_id=92))

    result = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="😀" * emoji_count,
    )

    assert result.payload_chars == expected_units
    assert result.ok is expected_ok
    assert client.post.await_count == int(expected_ok)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_telegram_message_preserves_telegram_error_fields(caplog) -> None:
    response = MagicMock()
    response.status_code = 400
    response.json = MagicMock(
        return_value={
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message is too long",
        }
    )
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with caplog.at_level(logging.ERROR):
        result = await transports.send_telegram_message(
            http_client=client,
            bot_token="secret-token",
            chat_id="1",
            text="safe summary",
        )

    assert result.ok is False
    assert result.status_code == 400
    assert result.error_code == 400
    assert result.error_classification is (TelegramErrorClassification.PAYLOAD_TOO_LONG)
    assert not hasattr(result, "description")
    record = next(
        item for item in caplog.records if item.msg == "telegram.method.failed"
    )
    assert record.http_status == 400
    assert record.telegram_error_code == 400
    assert record.telegram_error_classification == "payload_too_long"
    assert not hasattr(record, "telegram_description")
    assert "Bad Request: message is too long" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.unit
def test_httpx_telegram_bot_url_logging_is_redacted(caplog) -> None:
    token = "123456:top-secret"
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: POST https://api.telegram.org/bot%s/sendMessage "400"',
            token,
        )

    assert token not in caplog.text
    assert "bot[REDACTED]/sendMessage" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_answer_callback_query_omits_absent_text() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response())

    result = await transports.answer_callback_query(
        http_client=client,
        bot_token="T",
        callback_query_id="callback-1",
    )

    assert result is True
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.telegram.org/botT/answerCallbackQuery"
    assert kwargs["json"] == {"callback_query_id": "callback-1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_edit_message_text_forwards_reply_markup() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_telegram_response())
    keyboard = {"inline_keyboard": []}

    result = await transports.edit_message_text(
        http_client=client,
        bot_token="T",
        chat_id="1",
        message_id=42,
        text="approved",
        reply_markup=keyboard,
    )

    assert result.ok is True
    assert result.message_id == 42
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.telegram.org/botT/editMessageText"
    assert kwargs["json"] == {
        "chat_id": "1",
        "message_id": 42,
        "text": "approved",
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    }


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["answer", "edit"])
async def test_boolean_transport_helpers_return_false_on_failure(helper: str) -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("network error"))

    if helper == "answer":
        result = await transports.answer_callback_query(
            http_client=client,
            bot_token="T",
            callback_query_id="callback-1",
        )
    else:
        result = await transports.edit_message_text(
            http_client=client,
            bot_token="T",
            chat_id="1",
            message_id=42,
            text="approved",
        )

    if helper == "answer":
        assert result is False
    else:
        assert result.ok is False
        assert result.error_classification is (
            TelegramErrorClassification.TRANSPORT_ERROR
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_notifier_helpers_reuse_singleton_resources(
    notifier: TradeNotifier,
) -> None:
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _telegram_response(message_id=77),
            _telegram_response(),
            _telegram_response(),
        ]
    )
    notifier._http_client = client
    notifier._bot_token = "T"
    keyboard = {"inline_keyboard": [[{"text": "승인", "callback_data": "op:x:y"}]]}

    dispatch = await notifier.send_approval_message("approve?", keyboard, chat_id="1")
    answered = await notifier.answer_callback("callback-1", "received")
    edited = await notifier.edit_message(
        "1", 77, "approved", reply_markup={"inline_keyboard": []}
    )

    assert dispatch.ok is True
    assert dispatch.message_id == 77
    assert answered is True
    assert edited.ok is True
    first_call, second_call, third_call = client.post.call_args_list
    assert "botT/sendMessage" in first_call.args[0]
    assert first_call.kwargs["json"]["reply_markup"] == keyboard
    assert "botT/answerCallbackQuery" in second_call.args[0]
    assert second_call.kwargs["json"] == {
        "callback_query_id": "callback-1",
        "text": "received",
    }
    assert "botT/editMessageText" in third_call.args[0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["http_client", "bot_token"])
async def test_notifier_helpers_fail_safely_when_resource_missing(
    notifier: TradeNotifier,
    missing: str,
) -> None:
    client = MagicMock()
    client.post = AsyncMock()
    notifier._http_client = None if missing == "http_client" else client
    notifier._bot_token = None if missing == "bot_token" else "T"

    dispatch = await notifier.send_approval_message(
        "approve?", {"inline_keyboard": []}, chat_id="1"
    )
    assert dispatch.ok is False
    assert dispatch.failure_code == "telegram_notifier_unconfigured"
    assert await notifier.answer_callback("callback-1") is False
    edited = await notifier.edit_message("1", 42, "approved")
    assert edited.ok is False
    assert edited.error_classification is TelegramErrorClassification.NOT_CONFIGURED
    client.post.assert_not_awaited()
