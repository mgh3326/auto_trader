from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class TossApiErrorBase(RuntimeError):
    """Base class for Toss Open API adapter errors."""


class TossApiDisabled(TossApiErrorBase):
    """Raised when Toss API use is attempted while the env gate is disabled."""


class TossMissingCredentials(TossApiErrorBase):
    """Raised when Toss API credentials are missing."""


class TossHostBlocked(TossApiErrorBase):
    """Raised when a Toss request would leave the allowed Open API host."""


class TossTokenIssuanceUnavailable(TossApiErrorBase):
    """Raised when a contended OAuth issuance never publishes a token."""


@dataclass(frozen=True)
class TossErrorEnvelope:
    request_id: str | None
    code: str
    message: str
    data: dict[str, Any] | None = field(default=None)


class TossApiResponseError(TossApiErrorBase):
    def __init__(self, envelope: TossErrorEnvelope, *, status_code: int) -> None:
        self.envelope = envelope
        self.status_code = status_code
        super().__init__(
            f"Toss API error status={status_code} code={envelope.code!r} "
            f"request_id={envelope.request_id!r}"
        )


class TossRateLimitError(TossApiResponseError):
    """Raised for Toss 429 responses."""


def _parse_error_envelope(payload: dict[str, Any]) -> TossErrorEnvelope:
    raw_error = payload.get("error")
    if not isinstance(raw_error, dict):
        return TossErrorEnvelope(
            request_id=None,
            code="malformed-error",
            message="Toss error response did not contain an error object",
            data=None,
        )
    request_id = raw_error.get("requestId")
    code = raw_error.get("code")
    message = raw_error.get("message", "")
    data = raw_error.get("data")
    return TossErrorEnvelope(
        request_id=str(request_id) if request_id is not None else None,
        code=str(code or "unknown-error"),
        message=str(message or ""),
        data=data if isinstance(data, dict) else None,
    )


def parse_toss_response(response: httpx.Response) -> Any:
    payload = response.json()
    if 200 <= response.status_code < 300:
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload
    envelope = _parse_error_envelope(payload if isinstance(payload, dict) else {})
    if response.status_code == 429:
        raise TossRateLimitError(envelope, status_code=response.status_code)
    raise TossApiResponseError(envelope, status_code=response.status_code)
