"""Cloudflare Access assertion verification for the MCP HTTP route (hk 3592).

Operator decision ``hk:doc decision/2026-11-08/at-mcp-dots-all-tools``: dots
(a ChatGPT session) reaches the DEFAULT MCP at ``at-mcp.robinco.dev`` behind a
Cloudflare Access application whose policy is the operator's email.

Where the credential is checked
-------------------------------
Access does the OAuth dance at the edge (discovery documents, the 401
``WWW-Authenticate`` challenge, token issue) and forwards an authenticated
request to the origin with a signed ``Cf-Access-Jwt-Assertion`` header. FastMCP's
``TokenVerifier`` only sees the ``Authorization: Bearer`` string, which on that
route is the edge's own access token (and may or may not be forwarded), so it
cannot be the check. The check is therefore an auth *provider* whose Starlette
backend sees the whole connection: :class:`DualCredentialAuth`. Both transports
(streamable-http and sse) build their auth middleware and ``RequireAuthMiddleware``
from the provider, so this one decision function covers both.

Credential decision (:meth:`DualCredentialAuth.authenticate_connection`):

* ``Cf-Access-Jwt-Assertion`` present  -> it must verify, else refuse. A bearer
  token next to a bad assertion is not consulted.
* header absent                        -> the existing ``MCP_AUTH_TOKEN`` bearer
  check, unchanged (``DebugTokenVerifier`` + ``BearerAuthBackend``).
* neither                              -> no ``user``; ``RequireAuthMiddleware`` 401s.

``Cf-Access-Authenticated-User-Email`` and every other header are never read.

All Access configuration is off unless set; partially set refuses to boot.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.debug import DebugTokenVerifier
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    BearerAuthBackend,
)
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection

logger = logging.getLogger(__name__)

ENV_TEAM_DOMAIN = "MCP_CF_ACCESS_TEAM_DOMAIN"
ENV_AUD = "MCP_CF_ACCESS_AUD"
ENV_ALLOWED_EMAILS = "MCP_CF_ACCESS_ALLOWED_EMAILS"
CF_ACCESS_ENV_NAMES = (ENV_TEAM_DOMAIN, ENV_AUD, ENV_ALLOWED_EMAILS)

ASSERTION_HEADER = "cf-access-jwt-assertion"
# Attribution for every Access-authenticated call. ``dots`` is the consumer in
# Sentry and in proposals' ``creator_agent_id``; the operator email stays out of
# both and appears only in the invocation log line.
ACCESS_CONSUMER = "dots"
ACCESS_CALLER_SOURCE = "cf_access"

_TEAM_DOMAIN_SUFFIX = ".cloudflareaccess.com"
_TEAM_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")
_AUD_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+$")
_MAX_ASSERTION_CHARS = 8192
_CLOCK_SKEW_SECONDS = 30
_JWKS_TTL_SECONDS = 600.0
_UNKNOWN_KID_REFRESH_INTERVAL_SECONDS = 60.0
_JWKS_TIMEOUT_SECONDS = 3.0
_JWKS_FAILURE_BACKOFF_SECONDS = 10.0


class CfAccessConfigError(RuntimeError):
    """Cloudflare Access configuration is partial or malformed (refuse to boot)."""


def _ascii_lower(value: str) -> str:
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in value)


@dataclass(frozen=True)
class CfAccessConfig:
    team_domain: str
    aud: str
    allowed_emails: frozenset[str]

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"https://{self.team_domain}/cdn-cgi/access/certs"


def parse_cf_access_config(environ: Mapping[str, str]) -> CfAccessConfig | None:
    """Return the Access config, ``None`` when all three variables are unset.

    Blank counts as unset. Any one set without the other two, or any malformed
    value, raises :class:`CfAccessConfigError`; it is never read as "off".
    """
    raw = {name: (environ.get(name) or "").strip() for name in CF_ACCESS_ENV_NAMES}
    if not any(raw.values()):
        return None
    missing = [name for name, value in raw.items() if not value]
    if missing:
        raise CfAccessConfigError(
            "Cloudflare Access config is partial; also set: " + ", ".join(missing)
        )

    team = _ascii_lower(raw[ENV_TEAM_DOMAIN])
    if not _TEAM_DOMAIN_RE.fullmatch(team) or not team.endswith(_TEAM_DOMAIN_SUFFIX):
        raise CfAccessConfigError(
            f"{ENV_TEAM_DOMAIN} must be a bare <team>{_TEAM_DOMAIN_SUFFIX} host name"
        )
    if team == _TEAM_DOMAIN_SUFFIX.lstrip("."):
        raise CfAccessConfigError(f"{ENV_TEAM_DOMAIN} is missing the team label")
    if not _AUD_RE.fullmatch(raw[ENV_AUD]):
        raise CfAccessConfigError(f"{ENV_AUD} is not a valid Access application AUD")

    emails: set[str] = set()
    for item in raw[ENV_ALLOWED_EMAILS].split(","):
        email = _ascii_lower(item.strip())
        if not email:
            continue
        if not _EMAIL_RE.fullmatch(email):
            raise CfAccessConfigError(
                f"{ENV_ALLOWED_EMAILS} entries must be exact email addresses "
                "(no wildcards, no domains)"
            )
        emails.add(email)
    if not emails:
        raise CfAccessConfigError(f"{ENV_ALLOWED_EMAILS} lists no email address")
    return CfAccessConfig(
        team_domain=team, aud=raw[ENV_AUD], allowed_emails=frozenset(emails)
    )


@dataclass(frozen=True)
class CfAccessIdentity:
    email: str


class CfAccessGrant(AccessToken):
    """Marks a connection authenticated by a verified Access assertion.

    Carries no part of the assertion: ``token`` is a fixed placeholder.
    """


class CfAccessVerifier:
    """Verifies ``Cf-Access-Jwt-Assertion`` values; never raises on bad input."""

    def __init__(
        self,
        config: CfAccessConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        certs_url: str | None = None,
        ttl_seconds: float = _JWKS_TTL_SECONDS,
        unknown_kid_interval_seconds: float = _UNKNOWN_KID_REFRESH_INTERVAL_SECONDS,
        monotonic=time.monotonic,
    ) -> None:
        self._config = config
        self._client = http_client
        self._certs_url = certs_url or config.certs_url
        self._ttl = ttl_seconds
        self._unknown_interval = unknown_kid_interval_seconds
        self._monotonic = monotonic
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._last_unknown_refresh: float | None = None
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    async def verify(self, assertion: str) -> CfAccessIdentity | None:
        """Return the allowlisted identity, or ``None`` for any refusal."""
        raw = assertion.strip()
        if not raw or len(raw) > _MAX_ASSERTION_CHARS:
            return None
        try:
            header = jwt.get_unverified_header(raw)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                return None
            key = await self._key_for(kid)
            if key is None:
                return None
            claims = jwt.decode(
                raw,
                key=key,
                algorithms=["RS256"],
                audience=self._config.aud,
                issuer=self._config.issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as exc:  # noqa: BLE001 - every failure is one refusal
            # Class name only: exception text can echo token material.
            logger.warning("mcp.cf_access.refused reason=%s", type(exc).__name__)
            return None
        email = claims.get("email")
        if not isinstance(email, str):
            return None
        normalized = _ascii_lower(email.strip())
        if normalized not in self._config.allowed_emails:
            return None
        return CfAccessIdentity(email=normalized)

    async def _key_for(self, kid: str) -> Any | None:
        async with self._lock:
            now = self._monotonic()
            keys = self._keys
            fresh = keys is not None and now - self._fetched_at < self._ttl
            key = keys.get(kid) if keys is not None else None
            if key is not None and fresh:
                return key
            if key is None and keys is not None:
                # Unknown kid with a loaded key set: refresh at most once per
                # interval so a flood of forged kids cannot hammer the certs URL.
                # The very first load (no key set yet) is not rate limited.
                last = self._last_unknown_refresh
                if last is not None and now - last < self._unknown_interval:
                    return None
                self._last_unknown_refresh = now
            # Stale known key, or unknown kid outside the rate limit: refresh.
            # A failed refresh never falls back to a stale key, and backs off so
            # an unreachable certs URL costs one 3 s attempt per interval, not
            # one per request.
            if now < self._retry_after:
                return None
            if not await self._refresh():
                self._retry_after = self._monotonic() + _JWKS_FAILURE_BACKOFF_SECONDS
                return None
            return self._keys.get(kid) if self._keys is not None else None

    async def _refresh(self) -> bool:
        try:
            if self._client is not None:
                response = await self._client.get(self._certs_url)
            else:
                async with httpx.AsyncClient(
                    timeout=_JWKS_TIMEOUT_SECONDS, follow_redirects=False
                ) as client:
                    response = await client.get(self._certs_url)
            if response.status_code != 200:
                return False
            media_type = response.headers.get("content-type", "").split(";")[0]
            if media_type.strip().lower() != "application/json":
                return False
            documents = response.json().get("keys")
            keys: dict[str, Any] = {}
            for jwk in documents if isinstance(documents, list) else []:
                if (
                    isinstance(jwk, dict)
                    and jwk.get("kty") == "RSA"
                    and isinstance(jwk.get("kid"), str)
                    and jwk["kid"]
                ):
                    try:
                        keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                    except Exception:  # noqa: BLE001 - skip one malformed key
                        continue
            if not keys:
                return False
        except Exception as exc:  # noqa: BLE001 - JWKS failure refuses, never raises
            logger.warning("mcp.cf_access.jwks_failed reason=%s", type(exc).__name__)
            return False
        self._keys = keys
        self._fetched_at = self._monotonic()
        return True


class _DualBackend(AuthenticationBackend):
    def __init__(self, provider: DualCredentialAuth) -> None:
        self._provider = provider
        self._bearer = BearerAuthBackend(provider.bearer)

    async def authenticate(self, conn: HTTPConnection):
        assertion = conn.headers.get(ASSERTION_HEADER)
        if assertion is None:
            return await self._bearer.authenticate(conn)
        identity = await self._provider.verifier.verify(assertion)
        if identity is None:
            return None
        grant = CfAccessGrant(
            token="cf-access-assertion",
            client_id=f"operator:{identity.email}",
            scopes=[],
        )
        return AuthCredentials([]), AuthenticatedUser(grant)


class DualCredentialAuth(TokenVerifier):
    """Existing bearer token OR a verified Access assertion; nothing else."""

    def __init__(self, bearer: DebugTokenVerifier, verifier: CfAccessVerifier) -> None:
        super().__init__()
        self.bearer = bearer
        self.verifier = verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.bearer.verify_token(token)

    def get_middleware(self) -> list:
        return [
            Middleware(AuthenticationMiddleware, backend=_DualBackend(self)),  # type: ignore[arg-type]
            Middleware(AuthContextMiddleware),  # type: ignore[arg-type]
        ]


def build_dual_credential_auth(
    token: str, config: CfAccessConfig, **verifier_kwargs: Any
) -> DualCredentialAuth:
    expected = token.strip().encode()
    bearer = DebugTokenVerifier(
        validate=lambda t: hmac.compare_digest(expected, t.encode())
    )
    return DualCredentialAuth(bearer, CfAccessVerifier(config, **verifier_kwargs))


def access_operator_from_scope(scope: Mapping[str, Any] | None) -> str | None:
    """Return the verified operator email for this request, else ``None``."""
    if not scope:
        return None
    user = scope.get("user")
    if not isinstance(user, AuthenticatedUser):
        return None
    grant = user.access_token
    if not isinstance(grant, CfAccessGrant):
        return None
    return grant.client_id.removeprefix("operator:")
