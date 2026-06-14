from __future__ import annotations

import httpx
import pytest

from app.services.brokers.toss.errors import (
    TossApiResponseError,
    TossErrorEnvelope,
    TossRateLimitError,
    parse_toss_response,
)


def _response(status_code: int, payload: dict, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/accounts")
    return httpx.Response(
        status_code, json=payload, headers=headers or {}, request=request
    )


def test_parse_toss_response_returns_result() -> None:
    response = _response(200, {"result": {"accounts": []}})

    assert parse_toss_response(response) == {"accounts": []}


def test_parse_toss_response_allows_message_empty_and_unknown_code() -> None:
    response = _response(
        422,
        {
            "error": {
                "requestId": "req-1",
                "code": "new-unknown-code",
                "message": "",
                "data": {"tickSize": "5", "nearestPrices": ["100", "105"]},
            }
        },
    )

    with pytest.raises(TossApiResponseError) as exc_info:
        parse_toss_response(response)

    envelope = exc_info.value.envelope
    assert envelope == TossErrorEnvelope(
        request_id="req-1",
        code="new-unknown-code",
        message="",
        data={"tickSize": "5", "nearestPrices": ["100", "105"]},
    )
    assert "tickSize" not in str(exc_info.value)


def _text_response(status_code: int, text: str, headers: dict[str, str] | None = None):
    request = httpx.Request("GET", "https://openapi.tossinvest.com/api/v1/accounts")
    return httpx.Response(
        status_code, text=text, headers=headers or {}, request=request
    )


def test_parse_toss_response_non_json_error_synthesizes_typed_envelope() -> None:
    """ROB-547: a 5xx HTML/LB page must surface as a typed TossApiResponseError
    carrying status + code, not a raw JSONDecodeError."""
    response = _text_response(
        503,
        "<html><body>503 Service Unavailable</body></html>",
        headers={"cf-ray": "ray-123"},
    )

    with pytest.raises(TossApiResponseError) as exc_info:
        parse_toss_response(response)

    assert exc_info.value.status_code == 503
    assert exc_info.value.envelope.code == "non-json-response"
    assert exc_info.value.envelope.request_id == "ray-123"


def test_parse_toss_response_non_json_success_raises_typed() -> None:
    """A 2xx with a non-JSON body is unexpected for this API; surface typed."""
    response = _text_response(200, "")

    with pytest.raises(TossApiResponseError) as exc_info:
        parse_toss_response(response)

    assert exc_info.value.envelope.code == "non-json-response"


def test_parse_toss_response_429_raises_rate_limit_error() -> None:
    response = _response(
        429,
        {
            "error": {
                "requestId": "req-2",
                "code": "too-many-requests",
                "message": "slow down",
                "data": {"retryAfterSeconds": "1"},
            }
        },
    )

    with pytest.raises(TossRateLimitError):
        parse_toss_response(response)
