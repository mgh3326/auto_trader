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


class CallerInputError(ValueError):
    """Raised only where an MCP tool rejects caller-supplied arguments.

    Subclasses ``ValueError`` so existing ``except ValueError`` handlers and
    the caller-facing ``ToolError`` text are unchanged. Never raise this for
    internal invariants, provider/DB failures, or data-dependent problems —
    those stay plain ``ValueError`` (server faults, Sentry-visible).

    Lives in this leaf module (no imports) rather than
    ``app.mcp_server.caller_input_errors`` because pure helpers shared with
    ``app.services`` — e.g. ``app.mcp_server.tick_size`` — must raise it
    without pulling the MCP middleware stack into service import closures
    (tests/services/daily_plan boundary guards).
    """


__all__ = [
    "DomainServiceError",
    "SymbolNotFoundError",
    "RateLimitError",
    "UpstreamUnavailableError",
    "ValidationError",
    "CallerInputError",
]
