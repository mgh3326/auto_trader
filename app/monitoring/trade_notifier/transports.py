# app/monitoring/trade_notifier/transports.py
"""HTTP transport functions for Telegram and Discord delivery."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.telegram_contract import (
    TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
    TelegramErrorClassification,
    TelegramMethodResult,
    classify_telegram_response_error,
    telegram_text_length,
)

logger = logging.getLogger(__name__)

_TELEGRAM_BOT_URL_RE = re.compile(
    r"(?P<prefix>https?://api\.telegram\.org/bot)[^/\s\"']+",
    flags=re.IGNORECASE,
)
_TELEGRAM_BOT_PATH_RE = re.compile(
    r"(?P<prefix>/bot)[^/\s\"']+(?P<suffix>/(?:sendMessage|editMessageText|answerCallbackQuery))",
    flags=re.IGNORECASE,
)


def _redact_telegram_token(value: Any) -> Any:
    if isinstance(value, str):
        redacted = _TELEGRAM_BOT_URL_RE.sub(r"\g<prefix>[REDACTED]", value)
        return _TELEGRAM_BOT_PATH_RE.sub(r"\g<prefix>[REDACTED]\g<suffix>", redacted)
    if isinstance(value, bytes):
        return _redact_telegram_token(value.decode("utf-8", errors="replace")).encode()
    if isinstance(value, tuple):
        return tuple(_redact_telegram_token(item) for item in value)
    if isinstance(value, list):
        return [_redact_telegram_token(item) for item in value]
    if isinstance(value, dict):
        return {
            _redact_telegram_token(key): _redact_telegram_token(item)
            for key, item in value.items()
        }
    return value


class _TelegramTokenRedactionFilter(logging.Filter):
    """Remove Telegram bot credentials from dependency-generated URL logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - defensive logging boundary
            rendered = None
        if isinstance(rendered, str):
            redacted = _redact_telegram_token(rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
                return True
        record.msg = _redact_telegram_token(record.msg)
        record.args = _redact_telegram_token(record.args)
        return True


_token_redaction_filter = _TelegramTokenRedactionFilter()
for _dependency_logger_name in (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    _dependency_logger = logging.getLogger(_dependency_logger_name)
    if not any(
        isinstance(existing, _TelegramTokenRedactionFilter)
        for existing in _dependency_logger.filters
    ):
        _dependency_logger.addFilter(_token_redaction_filter)


def _safe_response_fields(
    response: httpx.Response,
) -> tuple[
    int,
    int | None,
    bool,
    int | None,
    TelegramErrorClassification,
]:
    """Extract only numeric/boolean success fields; discard description."""
    status_code = int(response.status_code)
    try:
        decoded = response.json()
    except (TypeError, ValueError):
        decoded = {}
    body = decoded if isinstance(decoded, dict) else {}
    raw_error_code = body.get("error_code")
    error_code = (
        raw_error_code
        if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool)
        else None
    )
    result_body = body.get("result")
    message_id = (
        result_body.get("message_id") if isinstance(result_body, dict) else None
    )
    safe_message_id = (
        message_id
        if isinstance(message_id, int) and not isinstance(message_id, bool)
        else None
    )
    classification = classify_telegram_response_error(
        status_code=status_code,
        error_code=error_code,
        description=body.get("description"),
    )
    return (
        status_code,
        error_code,
        body.get("ok") is True,
        safe_message_id,
        classification,
    )


def _log_method_failure(result: TelegramMethodResult, *, telegram_method: str) -> None:
    logger.error(
        "telegram.method.failed",
        extra={
            "telegram_method": telegram_method,
            "http_status": result.status_code,
            "telegram_error_code": result.error_code,
            "telegram_error_classification": (
                result.error_classification.value
                if result.error_classification is not None
                else None
            ),
            "payload_chars": result.payload_chars,
            "failure_code": result.failure_code,
        },
    )


async def send_telegram(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_ids: list[str],
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """Send a message to multiple Telegram chat IDs.

    Returns True if at least one chat received the message.
    """
    any_success = False
    for chat_id in chat_ids:
        result = await send_telegram_message(
            http_client=http_client,
            bot_token=bot_token,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        any_success = any_success or result.ok
    return any_success


async def send_telegram_message(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> TelegramMethodResult:
    """Send one message and preserve only allowlisted response metadata."""
    payload_chars = telegram_text_length(text)
    if payload_chars > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT:
        result = TelegramMethodResult.failed(
            payload_chars=payload_chars,
            failure_code="telegram_payload_too_long",
            error_classification=TelegramErrorClassification.PAYLOAD_TOO_LONG,
        )
        _log_method_failure(result, telegram_method="sendMessage")
        return result

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = await http_client.post(url, json=payload)
    except Exception:  # noqa: BLE001 - converted to a safe typed result
        result = TelegramMethodResult.failed(
            payload_chars=payload_chars,
            failure_code="telegram_transport_error",
            error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
        )
        _log_method_failure(result, telegram_method="sendMessage")
        return result

    (
        status_code,
        error_code,
        telegram_ok,
        message_id,
        classification,
    ) = _safe_response_fields(response)
    if 200 <= status_code < 300 and telegram_ok and message_id is not None:
        result = TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=status_code,
            error_code=None,
            error_classification=None,
            payload_chars=payload_chars,
        )
        logger.info(
            "telegram.send_message.sent",
            extra={
                "telegram_method": "sendMessage",
                "http_status": status_code,
                "payload_chars": payload_chars,
            },
        )
        return result

    failure_code = (
        "telegram_api_error" if error_code is not None else "telegram_invalid_response"
    )
    if error_code is None and 200 <= status_code < 300:
        classification = TelegramErrorClassification.INVALID_RESPONSE
    result = TelegramMethodResult.failed(
        payload_chars=payload_chars,
        failure_code=failure_code,
        status_code=status_code,
        error_code=error_code,
        error_classification=classification,
    )
    _log_method_failure(result, telegram_method="sendMessage")
    return result


async def answer_callback_query(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    callback_query_id: str,
    text: str | None = None,
) -> bool:
    """Acknowledge a Telegram callback query."""
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text

    try:
        response = await http_client.post(url, json=payload)
        response.raise_for_status()
        return True
    except Exception:
        logger.error("Failed to answer Telegram callback query")
        return False


async def edit_message_text(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> TelegramMethodResult:
    """Edit one message with the same UTF-16 and safe-result contract."""
    payload_chars = telegram_text_length(text)
    if payload_chars > TELEGRAM_SEND_MESSAGE_TEXT_LIMIT:
        result = TelegramMethodResult.failed(
            payload_chars=payload_chars,
            failure_code="telegram_payload_too_long",
            error_classification=TelegramErrorClassification.PAYLOAD_TOO_LONG,
        )
        _log_method_failure(result, telegram_method="editMessageText")
        return result

    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = await http_client.post(url, json=payload)
    except Exception:  # noqa: BLE001 - converted to a safe typed result
        result = TelegramMethodResult.failed(
            payload_chars=payload_chars,
            failure_code="telegram_transport_error",
            error_classification=TelegramErrorClassification.TRANSPORT_ERROR,
        )
        _log_method_failure(result, telegram_method="editMessageText")
        return result

    (
        status_code,
        error_code,
        telegram_ok,
        _message_id,
        classification,
    ) = _safe_response_fields(response)
    if 200 <= status_code < 300 and telegram_ok:
        return TelegramMethodResult(
            ok=True,
            message_id=message_id,
            status_code=status_code,
            error_code=None,
            error_classification=None,
            payload_chars=payload_chars,
        )
    if error_code is None and 200 <= status_code < 300:
        classification = TelegramErrorClassification.INVALID_RESPONSE
    result = TelegramMethodResult.failed(
        payload_chars=payload_chars,
        failure_code=(
            "telegram_api_error"
            if error_code is not None
            else "telegram_invalid_response"
        ),
        status_code=status_code,
        error_code=error_code,
        error_classification=classification,
    )
    _log_method_failure(result, telegram_method="editMessageText")
    return result


async def send_discord_embed_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    embed: dict[str, Any],
) -> bool:
    """Send a single Discord embed to one webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"embeds": [embed]},
        )
        response.raise_for_status()
        logger.info(f"Discord embed sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord embed to {webhook_url[:50]}...")
        return False


async def send_discord_content_single(
    *,
    http_client: httpx.AsyncClient,
    webhook_url: str,
    content: str,
) -> bool:
    """Send plain text content to one Discord webhook URL.

    Returns True on success, False on failure.
    """
    try:
        response = await http_client.post(
            webhook_url,
            json={"content": content},
        )
        response.raise_for_status()
        logger.info(f"Discord content sent to {webhook_url[:50]}...")
        return True
    except Exception:
        logger.error(f"Failed to send Discord content to {webhook_url[:50]}...")
        return False
