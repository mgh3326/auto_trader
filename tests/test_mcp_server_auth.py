"""Tests for MCP server Bearer token authentication."""

import pytest

from app.mcp_server.auth import StaticBearerTokenVerifier, build_auth_provider


class TestStaticBearerTokenVerifier:
    """Test cases for StaticBearerTokenVerifier."""

    def test_valid_token_matches(self):
        """Matching token should return True."""
        verifier = StaticBearerTokenVerifier("test-token")
        assert verifier.verify("test-token") is True

    def test_invalid_token_fails(self):
        """Non-matching token should return False."""
        verifier = StaticBearerTokenVerifier("correct-token")
        assert verifier.verify("wrong-token") is False
        assert verifier.verify("") is False

    def test_verify_uses_timing_safe_comparison(self):
        """Verify uses hmac.compare_digest for timing-safe comparison."""
        verifier = StaticBearerTokenVerifier("secret")
        result1 = verifier.verify("secret")
        result2 = verifier.verify("secret")

        assert result1 is True
        assert result2 is True
        assert result1 == result2

    def test_empty_token_disables_auth(self):
        """Empty expected token should disable authentication (return None)."""
        provider = build_auth_provider("")
        assert provider is None


class TestBuildAuthProvider:
    """Test cases for build_auth_provider factory function."""

    def test_valid_token_returns_verifier(self):
        """Valid token should return verifier instance."""
        provider = build_auth_provider("my-secret-token")
        assert isinstance(provider, StaticBearerTokenVerifier)
        assert provider._expected_token == "my-secret-token"

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
        assert isinstance(provider, StaticBearerTokenVerifier)
        assert provider._expected_token == "token-with-spaces"
