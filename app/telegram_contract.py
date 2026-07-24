"""Shared Telegram Bot API text-size and physical transport contracts.

The Bot API response ``description`` is deliberately absent from every public
type in this module.  It is untrusted remote input and must be discarded at
the HTTP boundary rather than sanitized after it has reached logs or ledgers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Telegram documents 1..4096 characters after entity parsing for sendMessage.
# Counting the raw payload in UTF-16 code units is deliberately conservative:
# Markdown delimiters/escapes are counted even though Telegram removes them
# while parsing, and astral characters count as two units instead of one.
TELEGRAM_SEND_MESSAGE_TEXT_LIMIT = 4096


def telegram_text_length(text: str) -> int:
    """Return the conservative Telegram text length (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def telegram_text_within_limit(text: str) -> bool:
    return telegram_text_length(text) <= TELEGRAM_SEND_MESSAGE_TEXT_LIMIT


def split_telegram_text(text: str, *, max_units: int) -> tuple[str, ...]:
    """Split text without dropping or rewriting a character."""
    if max_units <= 0:
        raise ValueError("max_units must be positive")
    if not text:
        return ("",)

    chunks: list[str] = []
    start = 0
    used_units = 0
    for index, character in enumerate(text):
        character_units = telegram_text_length(character)
        if character_units > max_units:
            raise ValueError("one character exceeds max_units")
        if used_units + character_units > max_units:
            chunks.append(text[start:index])
            start = index
            used_units = 0
        used_units += character_units
    chunks.append(text[start:])
    return tuple(chunks)


class TelegramErrorClassification(StrEnum):
    """Finite, non-sensitive classifications derived without response text."""

    PAYLOAD_TOO_LONG = "payload_too_long"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TRANSPORT_ERROR = "transport_error"
    INVALID_RESPONSE = "invalid_response"
    NOT_CONFIGURED = "not_configured"
    UNKNOWN_TELEGRAM_ERROR = "unknown_telegram_error"


def classify_telegram_error(
    *, status_code: int | None, error_code: int | None
) -> TelegramErrorClassification:
    """Classify a Telegram failure when no remote description was supplied."""
    numeric = error_code if error_code is not None else status_code
    if numeric == 400:
        return TelegramErrorClassification.BAD_REQUEST
    if numeric == 401:
        return TelegramErrorClassification.UNAUTHORIZED
    if numeric == 403:
        return TelegramErrorClassification.FORBIDDEN
    if numeric == 404:
        return TelegramErrorClassification.NOT_FOUND
    if numeric == 409:
        return TelegramErrorClassification.CONFLICT
    if numeric == 429:
        return TelegramErrorClassification.RATE_LIMITED
    if numeric is not None and 500 <= numeric <= 599:
        return TelegramErrorClassification.SERVER_ERROR
    return TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR


_KNOWN_TELEGRAM_DESCRIPTIONS: dict[str, TelegramErrorClassification] = {
    # Exact equality is intentional. Prefix/substring matching would let an
    # upstream reflector append request secrets while still influencing a
    # supposedly safe category.
    "Bad Request: message is too long": TelegramErrorClassification.PAYLOAD_TOO_LONG,
}


def classify_telegram_response_error(
    *,
    status_code: int | None,
    error_code: int | None,
    description: object,
) -> TelegramErrorClassification:
    """Collapse remote text to an allowlisted constant, then discard it.

    A present but unknown description is never generalized from its contents
    or copied onward. Numeric classification remains available for responses
    that omit ``description`` entirely.
    """
    if isinstance(description, str):
        return _KNOWN_TELEGRAM_DESCRIPTIONS.get(
            description,
            TelegramErrorClassification.UNKNOWN_TELEGRAM_ERROR,
        )
    return classify_telegram_error(status_code=status_code, error_code=error_code)


@dataclass(frozen=True, slots=True)
class TelegramMethodResult:
    """Physical result for one Bot API method.

    ``ok`` means only that this individual HTTP operation returned a valid
    Telegram success response.  Approval workflow success is a separate,
    durable state resolved after the current-attempt ownership fence.
    """

    ok: bool
    message_id: int | None
    status_code: int | None
    error_code: int | None
    error_classification: TelegramErrorClassification | None
    payload_chars: int
    failure_code: str | None = None

    @classmethod
    def failed(
        cls,
        *,
        payload_chars: int,
        failure_code: str,
        status_code: int | None = None,
        error_code: int | None = None,
        error_classification: TelegramErrorClassification | None = None,
    ) -> TelegramMethodResult:
        return cls(
            ok=False,
            message_id=None,
            status_code=status_code,
            error_code=error_code,
            error_classification=error_classification,
            payload_chars=payload_chars,
            failure_code=failure_code,
        )
