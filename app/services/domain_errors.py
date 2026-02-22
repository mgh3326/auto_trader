class DomainServiceError(Exception):
    pass


class SymbolNotFoundError(DomainServiceError):
    pass


class RateLimitError(DomainServiceError):
    pass


class UpstreamUnavailableError(DomainServiceError):
    pass


class ValidationError(DomainServiceError):
    pass


__all__ = [
    "DomainServiceError",
    "SymbolNotFoundError",
    "RateLimitError",
    "UpstreamUnavailableError",
    "ValidationError",
]
