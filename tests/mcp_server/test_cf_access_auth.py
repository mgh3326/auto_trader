"""hk 3592: the MCP HTTP route accepts a verified Cloudflare Access assertion.

Every invariant (a)-(g) is driven through a real FastMCP ASGI app over httpx's
ASGI transport. Signing keys are generated locally and the JWKS endpoint is an
in-process stub, so nothing here touches the network or a database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import FastMCP
from fastmcp.server.auth import DebugTokenVerifier
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from app.mcp_server import caller_identity_middleware as identity_middleware
from app.mcp_server import tool_call_log_middleware
from app.mcp_server.auth import build_auth_provider
from app.mcp_server.caller_identity import get_caller_agent_id, get_caller_source
from app.mcp_server.caller_identity_middleware import (
    CALLER_AGENT_ID_HEADER,
    CallerIdentityMiddleware,
)
from app.mcp_server.cf_access import (
    CF_ACCESS_ENV_NAMES,
    ENV_ALLOWED_EMAILS,
    ENV_AUD,
    ENV_TEAM_DOMAIN,
    CfAccessConfig,
    CfAccessConfigError,
    CfAccessGrant,
    DualCredentialAuth,
    build_dual_credential_auth,
    parse_cf_access_config,
)
from app.mcp_server.lifecycle import register_health_route
from app.mcp_server.sentry_middleware import McpToolCallSentryMiddleware
from app.mcp_server.tool_call_log_middleware import ToolCallLogMiddleware

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TEAM = "unit-test.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "aud0123456789abcdef0123456789abcdef"
OPERATOR = "operator@example.test"
BEARER = "tailnet-bearer-token"
KID = "kid-current"
TRADER_AGENT_ID = "trader-agent-from-allowlist"

CONFIG = CfAccessConfig(team_domain=TEAM, aud=AUD, allowed_emails=frozenset({OPERATOR}))
ENVIRON = {
    ENV_TEAM_DOMAIN: TEAM,
    ENV_AUD: AUD,
    ENV_ALLOWED_EMAILS: OPERATOR,
}


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _new_key()
OTHER_KEY = _new_key()


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


def assertion(
    *,
    key: rsa.RSAPrivateKey = SIGNING_KEY,
    kid: str | None = KID,
    algorithm: str = "RS256",
    **claim_overrides: Any,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [AUD],
        "email": OPERATOR,
        "iat": now,
        "nbf": now,
        "exp": now + 300,
        "type": "app",
    }
    claims.update(claim_overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


@dataclass
class Harness:
    client: httpx.AsyncClient
    certs: dict[str, Any]
    calls: list[str] = field(default_factory=list)
    certs_hits: list[int] = field(default_factory=list)

    async def rpc(
        self, headers: dict[str, str] | None = None, *, method: str = "tools/call"
    ) -> httpx.Response:
        params: dict[str, Any] = {}
        if method == "tools/call":
            params = {"name": "probe", "arguments": {}}
        return await self.client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
                **(headers or {}),
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )

    def probe_text(self, response: httpx.Response) -> str:
        assert response.status_code == 200, response.text
        return response.json()["result"]["content"][0]["text"]


def _build_mcp(provider: Any, calls: list[str]) -> FastMCP:
    mcp = FastMCP(name="cf-access-test", auth=provider)

    @mcp.tool
    def probe() -> str:
        calls.append("probe")
        return f"{get_caller_agent_id()}|{get_caller_source()}"

    mcp.add_middleware(ToolCallLogMiddleware(profile="default"))
    mcp.add_middleware(CallerIdentityMiddleware())
    mcp.add_middleware(McpToolCallSentryMiddleware())
    register_health_route(mcp)
    return mcp


@asynccontextmanager
async def _harness(
    *,
    access: bool = True,
    token: str | None = BEARER,
    transport: str = "streamable-http",
) -> AsyncIterator[Harness]:
    certs: dict[str, Any] = {"keys": [_jwk(SIGNING_KEY, KID)]}
    hits: list[int] = []

    def serve_certs(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{TEAM}/cdn-cgi/access/certs"
        hits.append(1)
        return httpx.Response(200, json=certs)

    certs_client = httpx.AsyncClient(transport=httpx.MockTransport(serve_certs))
    if access:
        assert token is not None
        provider: Any = build_dual_credential_auth(
            token, CONFIG, http_client=certs_client
        )
    else:
        provider = build_auth_provider(token, environ={})
    calls: list[str] = []
    mcp = _build_mcp(provider, calls)
    if transport == "sse":
        app = mcp.http_app(transport="sse")
    else:
        app = mcp.http_app(
            transport="streamable-http", json_response=True, stateless_http=True
        )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://origin.test"
        ) as client:
            yield Harness(client=client, certs=certs, calls=calls, certs_hits=hits)
    await certs_client.aclose()


def _access(token: str | None = None) -> dict[str, str]:
    return {"cf-access-jwt-assertion": token if token is not None else assertion()}


def _bearer(token: str = BEARER) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# --------------------------------------------------------------- (a) closed door


async def test_a_request_without_a_valid_credential_never_reaches_a_tool() -> None:
    async with _harness() as h:
        refusals = [
            await h.rpc(),
            await h.rpc(_bearer("not-the-token")),
            # The edge's own OAuth access token is a JWT in Authorization; it is
            # not a credential for this origin.
            await h.rpc(_bearer(assertion())),
            await h.rpc({"cf-access-jwt-assertion": ""}),
            await h.rpc({"cf-access-jwt-assertion": "   "}),
            await h.rpc({"cf-access-jwt-assertion": "not.a.jwt"}),
        ]
        assert [r.status_code for r in refusals] == [401] * len(refusals)
        assert h.calls == []

        ok = await h.rpc(_access())
        assert h.probe_text(ok).startswith("dots|")
        assert h.calls == ["probe"]


async def test_a_health_stays_unauthenticated_and_tools_list_is_gated() -> None:
    async with _harness() as h:
        assert (await h.client.get("/health")).status_code == 200
        assert (await h.rpc(method="tools/list")).status_code == 401
        listed = await h.rpc(_access(), method="tools/list")
        assert listed.status_code == 200
        assert [t["name"] for t in listed.json()["result"]["tools"]] == ["probe"]


async def _sse_status(h: Harness, headers: dict[str, str] | None = None) -> int:
    """Status of GET /sse, or 0 if a stream opened and stayed open (auth passed)."""
    try:
        async with asyncio.timeout(5):
            async with h.client.stream("GET", "/sse", headers=headers) as response:
                return response.status_code
    except TimeoutError:
        return 0


async def test_a_sse_transport_uses_the_same_decision() -> None:
    async with _harness(transport="sse") as h:
        assert await _sse_status(h) == 401
        assert await _sse_status(h, _access(assertion(aud="other-aud"))) == 401
        url = "/messages/?session_id=00000000000000000000000000000000"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await h.client.post(url, json=body)).status_code == 401
        bad = await h.client.post(url, json=body, headers=_access(assertion(aud="x")))
        assert bad.status_code == 401
        # A verified assertion gets past auth (the handler then rejects the
        # made-up session id), which is the only thing asserted here.
        passed = await h.client.post(url, json=body, headers=_access())
        assert passed.status_code != 401


# ------------------------------------------------- (b) Access unset == main today


async def test_b_with_access_unset_every_request_is_decided_as_before() -> None:
    provider = build_auth_provider(BEARER, environ={})
    assert type(provider) is DebugTokenVerifier
    assert build_auth_provider(None, environ={}) is None
    assert build_auth_provider("  ", environ={}) is None
    # Blank variables are "unset", as compose files commonly pass them.
    blank = dict.fromkeys(CF_ACCESS_ENV_NAMES, "  ")
    assert type(build_auth_provider(BEARER, environ=blank)) is DebugTokenVerifier

    async with _harness(access=False) as h:
        assert (await h.rpc()).status_code == 401
        # A genuine Access assertion means nothing when Access is off.
        assert (await h.rpc(_access())).status_code == 401
        assert h.calls == []
        ok = await h.rpc({**_bearer(), **_access("garbage")})
        assert h.probe_text(ok) == "None|none"
        assert h.calls == ["probe"]


# --------------------------------------------------- (c) assertions that refuse

_BAD_ASSERTIONS: dict[str, Callable[[], str]] = {
    "bad_signature": lambda: assertion(key=OTHER_KEY),
    "wrong_aud": lambda: assertion(aud=["some-other-application-aud"]),
    "wrong_iss": lambda: assertion(iss="https://evil.cloudflareaccess.com"),
    "expired": lambda: assertion(exp=int(time.time()) - 3600),
    "not_yet_valid": lambda: assertion(nbf=int(time.time()) + 3600),
    "missing_exp": lambda: assertion(exp=None),
    "email_not_allowlisted": lambda: assertion(email="intruder@example.test"),
    "email_prefix_of_allowed": lambda: assertion(email="operator@example.test.evil"),
    "no_email_claim": lambda: assertion(email=None, common_name="service-token"),
    "email_not_a_string": lambda: assertion(email=["operator@example.test"]),
    "alg_hs256_public_key_confusion": lambda: jwt.encode(
        {"iss": ISSUER, "aud": [AUD], "email": OPERATOR, "exp": int(time.time()) + 99},
        "x" * 40,
        algorithm="HS256",
        headers={"kid": KID},
    ),
    "alg_none": lambda: jwt.encode(
        {"iss": ISSUER, "aud": [AUD], "email": OPERATOR, "exp": int(time.time()) + 99},
        None,
        algorithm="none",
        headers={"kid": KID},
    ),
    "missing_kid": lambda: assertion(kid=None),
    "unknown_kid": lambda: assertion(kid="kid-never-published"),
}


@pytest.mark.parametrize("name", sorted(_BAD_ASSERTIONS))
async def test_c_bad_assertion_is_refused_and_no_tool_runs(name: str) -> None:
    async with _harness() as h:
        refused = await h.rpc(_access(_BAD_ASSERTIONS[name]()))
        assert refused.status_code == 401
        # A valid bearer next to a bad assertion is not a way around it.
        refused_with_bearer = await h.rpc(
            {**_bearer(), **_access(_BAD_ASSERTIONS[name]())}
        )
        assert refused_with_bearer.status_code == 401
        assert h.calls == []


async def test_c_good_assertion_is_accepted_and_email_is_case_normalised() -> None:
    async with _harness() as h:
        mixed = assertion(email="  Operator@Example.TEST ")
        assert h.probe_text(await h.rpc(_access(mixed))) == "dots|cf_access"


async def test_c_unknown_kid_refreshes_keys_once_then_accepts_rotation() -> None:
    async with _harness() as h:
        assert h.probe_text(await h.rpc(_access())).startswith("dots")
        assert len(h.certs_hits) == 1
        # Cached: a second request does not refetch.
        await h.rpc(_access())
        assert len(h.certs_hits) == 1

        rotated = _new_key()
        h.certs["keys"] = [_jwk(rotated, "kid-rotated")]
        token = assertion(key=rotated, kid="kid-rotated")
        # Unknown kid triggers one refresh, which now publishes it.
        assert h.probe_text(await h.rpc(_access(token))).startswith("dots")
        assert len(h.certs_hits) == 2


async def test_c_unknown_kid_refreshes_are_rate_limited() -> None:
    async with _harness() as h:
        await h.rpc(_access())
        base = len(h.certs_hits)
        for _ in range(5):
            r = await h.rpc(_access(assertion(kid="kid-never-published")))
            assert r.status_code == 401
        assert len(h.certs_hits) - base == 1


async def test_c_failed_jwks_fetch_refuses_instead_of_trusting_anything() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    provider = build_dual_credential_auth(BEARER, CONFIG, http_client=client)
    assert await provider.verifier.verify(assertion()) is None
    await client.aclose()


async def test_c_failed_jwks_fetch_backs_off_instead_of_refetching_per_request() -> (
    None
):
    hits: list[int] = []

    def broken(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(503, text="down")

    now = [1000.0]
    client = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    provider = build_dual_credential_auth(
        BEARER, CONFIG, http_client=client, monotonic=lambda: now[0]
    )
    for _ in range(5):
        assert await provider.verifier.verify(assertion()) is None
    assert len(hits) == 1
    now[0] += 11.0
    assert await provider.verifier.verify(assertion()) is None
    assert len(hits) == 2
    await client.aclose()


# ------------------------------------------------------ (d) forged email header


async def test_d_email_header_alone_or_beside_a_bad_assertion_is_refused() -> None:
    async with _harness() as h:
        forged = {
            "cf-access-authenticated-user-email": OPERATOR,
            "x-forwarded-email": OPERATOR,
        }
        assert (await h.rpc(forged)).status_code == 401
        bad = {**forged, **_access(assertion(email="intruder@example.test"))}
        assert (await h.rpc(bad)).status_code == 401
        assert h.calls == []

        # Beside a valid bearer the header changes nothing: still the bearer path.
        ok = await h.rpc({**_bearer(), **forged})
        assert h.probe_text(ok) == "None|none"


# ----------------------------------------------------------- (e) partial config


@pytest.mark.parametrize(
    "present",
    [
        (ENV_TEAM_DOMAIN,),
        (ENV_AUD,),
        (ENV_ALLOWED_EMAILS,),
        (ENV_TEAM_DOMAIN, ENV_AUD),
        (ENV_TEAM_DOMAIN, ENV_ALLOWED_EMAILS),
        (ENV_AUD, ENV_ALLOWED_EMAILS),
    ],
)
async def test_e_partial_config_refuses_to_build(present: tuple[str, ...]) -> None:
    environ = {name: ENVIRON[name] for name in present}
    with pytest.raises(CfAccessConfigError, match="partial"):
        parse_cf_access_config(environ)
    with pytest.raises(CfAccessConfigError):
        build_auth_provider(BEARER, environ=environ)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (ENV_TEAM_DOMAIN, "https://unit-test.cloudflareaccess.com"),
        (ENV_TEAM_DOMAIN, "unit-test.cloudflareaccess.com/"),
        (ENV_TEAM_DOMAIN, "attacker.example.com"),
        (ENV_TEAM_DOMAIN, "cloudflareaccess.com"),
        (ENV_AUD, "short"),
        (ENV_ALLOWED_EMAILS, "*@example.test"),
        (ENV_ALLOWED_EMAILS, "example.test"),
        (ENV_ALLOWED_EMAILS, "a@b.test, *"),
        (ENV_ALLOWED_EMAILS, " , "),
    ],
)
async def test_e_malformed_config_refuses_to_build(name: str, value: str) -> None:
    with pytest.raises(CfAccessConfigError):
        parse_cf_access_config({**ENVIRON, name: value})


async def test_e_access_without_bearer_token_refuses_to_build() -> None:
    for token in (None, "", "   "):
        with pytest.raises(CfAccessConfigError, match="MCP_AUTH_TOKEN"):
            build_auth_provider(token, environ=ENVIRON)


async def test_e_full_env_builds_the_dual_provider() -> None:
    provider = build_auth_provider(BEARER, environ=ENVIRON)
    assert isinstance(provider, DualCredentialAuth)
    config = parse_cf_access_config(ENVIRON)
    assert config == CONFIG


def _boot(env_extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "MCP_PROFILE": "default",
        "MCP_AUTH_TOKEN": BEARER,
        "MCP_TYPE": "stdio",
        "SENTRY_DSN": "",
        **env_extra,
    }
    for name in CF_ACCESS_ENV_NAMES:
        if name not in env_extra:
            env.pop(name, None)
    return subprocess.run(  # noqa: S603 - fixed argv, test-only
        [sys.executable, "-c", "import app.mcp_server.main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


async def test_e_other_profiles_ignore_the_access_variables() -> None:
    for environ in (ENVIRON, {ENV_AUD: AUD}):
        provider = build_auth_provider(BEARER, environ=environ, cf_access_enabled=False)
        assert type(provider) is DebugTokenVerifier
    # Even a tokenless non-DEFAULT unit is not turned into a refusal by them.
    assert build_auth_provider(None, environ=ENVIRON, cf_access_enabled=False) is None


async def test_e_server_process_boots_or_refuses_by_config_and_profile() -> None:
    partial = _boot({ENV_AUD: AUD})
    assert partial.returncode != 0
    assert "CfAccessConfigError" in partial.stderr
    assert "partial" in partial.stderr
    assert AUD not in partial.stderr

    assert _boot(ENVIRON).returncode == 0

    # A fixed-profile unit sharing the env file starts normally and stays bearer-only.
    other_profile = _boot({**ENVIRON, "MCP_PROFILE": "crypto"})
    assert other_profile.returncode == 0, other_profile.stderr[-2000:]
    other_partial = _boot({ENV_AUD: AUD, "MCP_PROFILE": "crypto"})
    assert other_partial.returncode == 0, other_partial.stderr[-2000:]


# ------------------------------------------------------- (f) bearer unchanged

_TOKEN_MATRIX = [
    BEARER,
    f" {BEARER}",
    f"{BEARER} ",
    BEARER.upper(),
    BEARER[:-1],
    BEARER + "x",
    "",
    "Bearer",
    "ünïcode-token",
]


@pytest.mark.parametrize("candidate", _TOKEN_MATRIX)
async def test_f_bearer_decision_equals_the_pre_3592_verifier(candidate: str) -> None:
    before = build_auth_provider(f"  {BEARER}  ", environ={})
    after = build_auth_provider(f"  {BEARER}  ", environ=ENVIRON)
    assert isinstance(before, DebugTokenVerifier)
    assert isinstance(after, DualCredentialAuth)
    old = await before.verify_token(candidate)
    new = await after.verify_token(candidate)
    assert (old is None) == (new is None)


async def test_f_bearer_over_http_matches_with_and_without_access() -> None:
    async with _harness(access=False) as off, _harness(access=True) as on:
        for candidate in (BEARER, BEARER + "x", ""):
            headers = _bearer(candidate) if candidate else {}
            a = await off.rpc(headers)
            b = await on.rpc(headers)
            assert a.status_code == b.status_code
        assert off.calls == on.calls == ["probe"]


# -------------------------------------------------------- (g) identity, no secrets


async def test_g_access_call_is_dots_and_ignores_a_spoofed_agent_header(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []
    sentry = MagicMock(side_effect=lambda *a, **kw: recorded.append(kw))
    monkeypatch.setattr(
        "app.mcp_server.sentry_middleware.enrich_mcp_tool_call_scope", sentry
    )
    token = assertion()
    caplog.set_level(logging.INFO, logger=tool_call_log_middleware.__name__)
    async with _harness() as h:
        spoof = {CALLER_AGENT_ID_HEADER: TRADER_AGENT_ID}
        text = h.probe_text(await h.rpc({**_access(token), **spoof}))
        assert text == "dots|cf_access"

        # The tailnet bearer path keeps honouring the header, unchanged.
        via_bearer = h.probe_text(await h.rpc({**_bearer(), **spoof}))
        assert via_bearer == f"{TRADER_AGENT_ID}|http_header"

    lines = [r.getMessage() for r in caplog.records if "mcp.tool.called" in r.message]
    assert lines == [
        "mcp.tool.called tool=probe profile=default consumer=dots "
        f"operator=operator:{OPERATOR}",
        "mcp.tool.called tool=probe profile=default",
    ]
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in everything
    assert BEARER not in everything

    access_call = recorded[0]
    assert access_call["caller_agent_id"] == "dots"
    assert access_call["caller_source"] == "cf_access"
    assert OPERATOR not in repr(recorded[0])
    assert token not in repr(recorded)


async def test_g_header_cannot_override_a_verified_access_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolution itself, not just the HTTP path: the agent-id header is ignored."""
    grant = CfAccessGrant(
        token="cf-access-assertion", client_id="operator:x@y.test", scopes=[]
    )
    request = MagicMock()
    request.headers = {CALLER_AGENT_ID_HEADER: TRADER_AGENT_ID}
    request.scope = {"user": AuthenticatedUser(grant)}
    monkeypatch.setattr(identity_middleware, "get_http_request", lambda: request)
    assert identity_middleware._resolve_caller_identity() == ("dots", "cf_access")

    request.scope = {}
    assert identity_middleware._resolve_caller_identity() == (
        TRADER_AGENT_ID,
        "http_header",
    )
