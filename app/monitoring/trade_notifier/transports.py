# app/monitoring/trade_notifier/transports.py
"""HTTP transport functions for Telegram and Discord delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

import httpx

from app.telegram_contract import (
    TELEGRAM_SEND_MESSAGE_TEXT_LIMIT,
    TelegramErrorClassification,
    TelegramMethodResult,
    classify_telegram_response_error,
    split_telegram_text,
    telegram_text_length,
)

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_ATTEMPTS = 3
_TELEGRAM_RETRY_BASE_SECONDS = 0.25
_TELEGRAM_RETRY_MAX_SECONDS = 5.0
_TELEGRAM_BOT_URL_RE = re.compile(
    r"(?P<prefix>https?://api\.telegram\.org/bot)[^/\s\"']+",
    flags=re.IGNORECASE,
)
_TELEGRAM_PARSE_OFFSET_RE = re.compile(
    r"^Bad Request: can't parse entities:.*byte offset (?P<offset>\d+)$",
    flags=re.IGNORECASE,
)
_TELEGRAM_RETRY_DESCRIPTION_RE = re.compile(
    r"^Too Many Requests: retry after (?P<seconds>\d+)$",
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


def _telegram_chat_ref(chat_id: str) -> str:
    """Return a stable, non-reversible identifier for one configured chat."""
    digest = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _telegram_response_body_for_log(response: httpx.Response) -> dict[str, Any]:
    """Return diagnostic Telegram fields without propagating remote free text."""
    try:
        decoded = response.json()
    except (TypeError, ValueError):
        return {"body": "[non-json response redacted]"}
    if not isinstance(decoded, dict):
        return {"body": "[non-object response redacted]"}

    safe_body: dict[str, Any] = {}
    if isinstance(decoded.get("ok"), bool):
        safe_body["ok"] = decoded["ok"]
    error_code = decoded.get("error_code")
    if isinstance(error_code, int) and not isinstance(error_code, bool):
        safe_body["error_code"] = error_code

    description = decoded.get("description")
    if description == "Bad Request: message is too long":
        safe_body["description"] = description
    elif isinstance(description, str):
        parse_match = _TELEGRAM_PARSE_OFFSET_RE.fullmatch(description)
        retry_match = _TELEGRAM_RETRY_DESCRIPTION_RE.fullmatch(description)
        if parse_match:
            safe_body["description"] = (
                "Bad Request: can't parse entities "
                f"(byte_offset={parse_match.group('offset')})"
            )
        elif retry_match:
            safe_body["description"] = (
                f"Too Many Requests: retry after {retry_match.group('seconds')}"
            )
        else:
            safe_body["description"] = "[untrusted description redacted]"
    return safe_body


def _telegram_status_code(response: Any) -> int | None:
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code
    return None


def _telegram_retry_delay(response: httpx.Response, *, attempt: int) -> float:
    retry_after: float | None = None
    try:
        header_value = response.headers.get("Retry-After")
        if header_value is not None:
            retry_after = float(header_value)
    except (AttributeError, TypeError, ValueError):
        retry_after = None

    if retry_after is None:
        try:
            decoded = response.json()
        except (TypeError, ValueError):
            decoded = {}
        parameters = decoded.get("parameters") if isinstance(decoded, dict) else None
        raw_retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        if isinstance(raw_retry_after, (int, float)) and not isinstance(
            raw_retry_after, bool
        ):
            retry_after = float(raw_retry_after)

    if retry_after is None or retry_after < 0:
        retry_after = _TELEGRAM_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    return min(retry_after, _TELEGRAM_RETRY_MAX_SECONDS)


def _safe_exception_message(
    error: Exception,
    *,
    bot_token: str,
    chat_id: str,
) -> str:
    message = str(_redact_telegram_token(str(error)))
    if bot_token:
        message = message.replace(bot_token, "[REDACTED]")
    if chat_id:
        message = message.replace(chat_id, "[REDACTED_CHAT_ID]")
    return message


def _log_telegram_send_failure(
    *,
    chat_id: str,
    text: str,
    attempt: int,
    error: Exception,
    bot_token: str,
    response: httpx.Response | None,
    retryable: bool,
) -> None:
    chat_ref = _telegram_chat_ref(chat_id)
    exception_message = _safe_exception_message(
        error,
        bot_token=bot_token,
        chat_id=chat_id,
    )
    http_status = _telegram_status_code(response) if response is not None else None
    response_body = (
        _telegram_response_body_for_log(response) if response is not None else None
    )
    payload_chars = telegram_text_length(text)
    logger.error(
        "telegram.send.failed chat_ref=%s attempt=%d/%d exception_type=%s "
        "exception_message=%r http_status=%s response_body=%s retryable=%s "
        "payload_chars=%d",
        chat_ref,
        attempt,
        _TELEGRAM_MAX_ATTEMPTS,
        type(error).__name__,
        exception_message,
        http_status,
        response_body,
        retryable,
        payload_chars,
        extra={
            "chat_ref": chat_ref,
            "attempt": attempt,
            "max_attempts": _TELEGRAM_MAX_ATTEMPTS,
            "exception_type": type(error).__name__,
            "exception_message": exception_message,
            "http_status": http_status,
            "response_body": response_body,
            "retryable": retryable,
            "payload_chars": payload_chars,
        },
    )


async def _send_telegram_payload(
    *,
    http_client: httpx.AsyncClient,
    url: str,
    bot_token: str,
    chat_id: str,
    payload: dict[str, Any],
) -> bool:
    for attempt in range(1, _TELEGRAM_MAX_ATTEMPTS + 1):
        response: httpx.Response | None = None
        try:
            response = await http_client.post(url, json=payload)
            status_code = _telegram_status_code(response)
            if status_code is None:
                response.raise_for_status()
                return True
            if 200 <= status_code < 300:
                response.raise_for_status()
                return True
            try:
                response.raise_for_status()
            except Exception as error:  # noqa: BLE001 - logged and classified below
                failure = error
            else:
                failure = RuntimeError(f"Telegram API returned HTTP {status_code}")
        except Exception as error:  # noqa: BLE001 - bounded transport boundary
            failure = error
            if isinstance(error, httpx.HTTPStatusError):
                response = error.response

        status_code = _telegram_status_code(response) if response is not None else None
        retryable = isinstance(failure, httpx.TransportError) or (
            status_code == 429
            or (status_code is not None and 500 <= status_code <= 599)
        )
        if retryable and attempt < _TELEGRAM_MAX_ATTEMPTS:
            delay = (
                _telegram_retry_delay(response, attempt=attempt)
                if response is not None
                else min(
                    _TELEGRAM_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    _TELEGRAM_RETRY_MAX_SECONDS,
                )
            )
            logger.warning(
                "telegram.send.retrying",
                extra={
                    "chat_ref": _telegram_chat_ref(chat_id),
                    "attempt": attempt,
                    "max_attempts": _TELEGRAM_MAX_ATTEMPTS,
                    "exception_type": type(failure).__name__,
                    "http_status": status_code,
                    "retry_delay_seconds": delay,
                },
            )
            await asyncio.sleep(delay)
            continue

        _log_telegram_send_failure(
            chat_id=chat_id,
            text=payload["text"],
            attempt=attempt,
            error=failure,
            bot_token=bot_token,
            response=response,
            retryable=retryable,
        )
        return False
    return False  # pragma: no cover - loop always returns


async def send_telegram(
    *,
    http_client: httpx.AsyncClient,
    bot_token: str,
    chat_ids: list[str],
    text: str,
    parse_mode: str | None = "Markdown",
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    """Send a message to multiple Telegram chat IDs.

    Long messages are split at Telegram's conservative UTF-16 limit. Returns
    True only when every chunk reaches every configured chat.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = split_telegram_text(text, max_units=TELEGRAM_SEND_MESSAGE_TEXT_LIMIT)
    all_success = bool(chat_ids)
    for chat_id in chat_ids:
        chat_success = True
        for chunk_index, chunk in enumerate(chunks, start=1):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            sent = await _send_telegram_payload(
                http_client=http_client,
                url=url,
                bot_token=bot_token,
                chat_id=chat_id,
                payload=payload,
            )
            if not sent:
                chat_success = False
                break
            logger.info(
                "telegram.send.sent",
                extra={
                    "chat_ref": _telegram_chat_ref(chat_id),
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "payload_chars": telegram_text_length(chunk),
                },
            )
        all_success = all_success and chat_success
    return all_success


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
