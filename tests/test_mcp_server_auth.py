"""Tests for MCP server Bearer token authentication."""

from fastmcp.server.auth import DebugTokenVerifier

from app.mcp_server.auth import build_auth_provider


class TestBuildAuthProvider:
    """Test cases for build_auth_provider factory function."""

    def test_valid_token_returns_verifier(self):
        """Valid token should return DebugTokenVerifier instance."""
        provider = build_auth_provider("my-secret-token")
        assert isinstance(provider, DebugTokenVerifier)

    def test_none_returns_none(self):
        """None should return None (disabled auth)."""
        provider = build_auth_provider(None)
        assert provider is None

    def test_empty_string_returns_none(self):
        """Empty string should return None (disabled auth)."""
        provider = build_auth_provider("")
        assert provider is None

    def test_whitespace_only_returns_none(self):
        """Whitespace-only string should return None (disabled auth)."""
        provider = build_auth_provider("   ")
        assert provider is None

    def test_whitespace_token_returns_verifier(self):
        """Token with leading/trailing whitespace should be trimmed."""
        provider = build_auth_provider("  token-with-spaces  ")
        assert isinstance(provider, DebugTokenVerifier)

    def test_has_get_middleware(self):
        """Provider should expose get_middleware for FastMCP integration."""
        provider = build_auth_provider("test-token")
        assert hasattr(provider, "get_middleware")
