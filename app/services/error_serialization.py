from __future__ import annotations

from app.services.domain_errors import (
    DomainServiceError,
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)


def domain_error_status_code(exc: Exception) -> int:
    if isinstance(exc, ValidationError):
        return 400
    if isinstance(exc, SymbolNotFoundError):
        return 404
    if isinstance(exc, RateLimitError):
        return 429
    if isinstance(exc, UpstreamUnavailableError):
        return 503
    return 500


def serialize_domain_error(exc: Exception) -> dict[str, str]:
    return {
        "error_type": exc.__class__.__name__,
        "message": str(exc),
    }


def is_domain_error(exc: Exception) -> bool:
    return isinstance(exc, DomainServiceError)


__all__ = [
    "domain_error_status_code",
    "serialize_domain_error",
    "is_domain_error",
]
