"""Closed error vocabulary for the KRX Open API daily client (hk 6586).

Error messages carry only these closed codes. They never carry the auth key,
the response body, or provider text, so a failure cannot leak the secret.
"""

from __future__ import annotations


class KrxOpenApiError(RuntimeError):
    """Base failure. ``code`` is fixed per class; ``detail`` is a closed local tag."""

    code: str = "unexpected"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(self.code if detail is None else f"{self.code}:{detail}")


class KrxUnauthorizedKeyError(KrxOpenApiError):
    """The AUTH_KEY header was rejected (401 'Unauthorized Key')."""

    code = "unauthorized_key"


class KrxServiceNotApprovedError(KrxOpenApiError):
    """The key is valid but the service is not approved (401 'Unauthorized API Call')."""

    code = "service_not_approved"


class KrxHostRefusedError(KrxOpenApiError):
    """A URL outside the exact host allowlist, or a non-https URL, was refused."""

    code = "host_refused"


class KrxTransportError(KrxOpenApiError):
    """Timeout, transport failure, non-200 status, or a refused redirect."""

    code = "transport"


class KrxMalformedError(KrxOpenApiError):
    """Response body or content type did not match the documented shape."""

    code = "malformed"


CLOSED_CODES = frozenset(
    {
        "unauthorized_key",
        "service_not_approved",
        "host_refused",
        "transport",
        "malformed",
        "unexpected",
    }
)
