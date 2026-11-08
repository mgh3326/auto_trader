"""Authentication for the MCP server.

Credential 1 is the bearer ``MCP_AUTH_TOKEN`` (FastMCP's ``DebugTokenVerifier``
with a timing-safe comparison). Credential 2 (hk 3592) is a verified Cloudflare
Access assertion, off unless ``MCP_CF_ACCESS_*`` is configured; see
:mod:`app.mcp_server.cf_access`.
"""

import hmac
import os
from collections.abc import Mapping

from fastmcp.server.auth import DebugTokenVerifier

from app.mcp_server.cf_access import (
    ENV_TEAM_DOMAIN,
    CfAccessConfigError,
    DualCredentialAuth,
    build_dual_credential_auth,
    parse_cf_access_config,
)


def build_auth_provider(
    token: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    cf_access_enabled: bool = True,
) -> DebugTokenVerifier | DualCredentialAuth | None:
    """Build FastMCP auth provider from token string.

    Args:
        token: Bearer token string, or None to disable auth
        environ: Environment to read ``MCP_CF_ACCESS_*`` from (default: process env)
        cf_access_enabled: False for every profile except DEFAULT; the Access
            variables are then not read at all (units share one env file)

    Returns:
        DebugTokenVerifier instance if token provided and Access is not
        configured (exactly the pre-3592 behaviour), DualCredentialAuth when
        Access is configured, None to disable auth.

    Raises:
        CfAccessConfigError: Access config partial/malformed, or set without a
            bearer token (it would silently turn a tokenless server into
            Access-only for its existing tailnet callers).
    """
    access = (
        parse_cf_access_config(os.environ if environ is None else environ)
        if cf_access_enabled
        else None
    )
    has_token = bool(token and token.strip())
    if access is not None:
        if not has_token:
            raise CfAccessConfigError(
                f"{ENV_TEAM_DOMAIN} and the other MCP_CF_ACCESS_* variables require "
                "a non-empty MCP_AUTH_TOKEN"
            )
        assert token is not None
        return build_dual_credential_auth(token, access)

    if not has_token:
        return None
    assert token is not None
    expected = token.strip()

    def _validate(t: str) -> bool:
        return hmac.compare_digest(expected.encode(), t.encode())

    return DebugTokenVerifier(validate=_validate)
