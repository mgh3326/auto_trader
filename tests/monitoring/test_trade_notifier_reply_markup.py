"""Tests for Telegram inline-keyboard transport and notifier helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.monitoring.trade_notifier import TradeNotifier, transports


def _telegram_response(*, message_id: int = 42) -> MagicMock:
    response = MagicMock()
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

    message_id = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="approve?",
        reply_markup=keyboard,
    )

    assert message_id == 42
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
async def test_send_telegram_message_returns_none_on_failure() -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=RuntimeError("network error"))

    result = await transports.send_telegram_message(
        http_client=client,
        bot_token="T",
        chat_id="1",
        text="approve?",
    )

    assert result is None


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

    assert result is True
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

    assert result is False


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

    message_id = await notifier.send_approval_message("approve?", keyboard, chat_id="1")
    answered = await notifier.answer_callback("callback-1", "received")
    edited = await notifier.edit_message(
        "1", 77, "approved", reply_markup={"inline_keyboard": []}
    )

    assert message_id == 77
    assert answered is True
    assert edited is True
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

    assert (
        await notifier.send_approval_message(
            "approve?", {"inline_keyboard": []}, chat_id="1"
        )
        is None
    )
    assert await notifier.answer_callback("callback-1") is False
    assert await notifier.edit_message("1", 42, "approved") is False
    client.post.assert_not_awaited()
