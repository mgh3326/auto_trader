"""Typed, value-redacted failures for the NHPLUG mock read-only boundary."""

from __future__ import annotations


class NHPlugMockError(RuntimeError):
    """Base class for every NHPLUG mock read-only refusal or response failure."""


class NHPlugMockDisabled(NHPlugMockError):
    """Raised unless the explicit mock read gate is armed."""


class NHPlugMockConfigurationError(NHPlugMockError):
    """Raised for incomplete or malformed local read-only configuration."""


class NHPlugMockEndpointError(NHPlugMockError):
    """Raised when a request would leave the pinned mock endpoint."""


class NHPlugMockReadOnlyEndpointError(NHPlugMockError):
    """Raised before token resolution for a non-allowlisted data path."""


class NHPlugMockAccountRejected(NHPlugMockError):
    """Raised when the configured account is not a verified mock account."""


class NHPlugMockResponseError(NHPlugMockError):
    """Raised for a malformed broker response without echoing its body."""


class NHPlugMockBrokerRejected(NHPlugMockError):
    """A broker business rejection represented by its non-sensitive response code."""

    def __init__(self, *, response_code: str) -> None:
        self.response_code = response_code
        super().__init__(
            f"NHPLUG mock broker rejected the read request (code={response_code})"
        )
