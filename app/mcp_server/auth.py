"""Bearer token authentication for MCP server.

Uses FastMCP's built-in DebugTokenVerifier with timing-safe comparison.
"""

import hmac

from fastmcp.server.auth import DebugTokenVerifier


def build_auth_provider(token: str | None) -> DebugTokenVerifier | None:
    """Build FastMCP auth provider from token string.

    Args:
        token: Bearer token string, or None to disable auth

    Returns:
        DebugTokenVerifier instance if token provided, None to disable auth
    """
    if not token or not token.strip():
        return None

    expected = token.strip()

    def _validate(t: str) -> bool:
        return hmac.compare_digest(expected.encode(), t.encode())

    return DebugTokenVerifier(validate=_validate)
