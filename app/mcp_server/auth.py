"""Bearer token authentication for MCP server.

This module provides timing-safe Bearer token verification
compatible with FastMCP 2.14.4's auth parameter.
"""

import hmac


class StaticBearerTokenVerifier:
    """Static Bearer token verifier using timing-safe comparison.

    Uses hmac.compare_digest() for constant-time comparison to prevent
    timing attacks when comparing tokens.
    """

    def __init__(self, expected_token: str) -> None:
        """Initialize verifier with expected token.

        Args:
            expected_token: The valid Bearer token string
        """
        self._expected_token = expected_token

    def verify(self, token: str) -> bool:
        """Verify if the provided token matches expected token.

        Args:
            token: The token from Authorization header

        Returns:
            True if token matches, False otherwise
        """
        if not self._expected_token:
            return True

        return hmac.compare_digest(self._expected_token.encode(), token.encode())


def build_auth_provider(token: str | None):
    """Build FastMCP auth provider from token string.

    Args:
        token: Bearer token string, or None to disable auth

    Returns:
        StaticBearerTokenVerifier instance if token provided, None to disable auth

    Examples:
        >>> # With authentication
        >>> verifier = build_auth_provider("my-secret-token")

        >>> # Without authentication (disabled)
        >>> verifier = build_auth_provider(None)
        >>> verifier = build_auth_provider("")
    """
    if not token or not token.strip():
        return None

    return StaticBearerTokenVerifier(token.strip())
