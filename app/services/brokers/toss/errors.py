from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
