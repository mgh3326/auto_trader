"""ROB-1296 — the external-HTTP boundary blocks the network and nothing else.

The boundary is deliberately broad: one fixture in place of seven per-provider
stubs. That breadth is only safe if it is pinned, so these tests fix its exact
edges — real transports blocked, every in-process transport untouched, both
opt-out routes honoured, and a counter that makes a newly under-mocked provider
visible instead of silently fail-soft.

Nothing here performs a real request. The blocked cases raise before any socket
work, and the allowed cases run against in-process transports.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import requests

from tests import _external_http_boundary as boundary
from tests import _socket_guard as socket_guard

EXTERNAL_URL = "https://rob1296-boundary.invalid/probe"  # RFC 6761 reserved TLD
PROBE_PATH = (
    Path(__file__).parent / "_socket_guard_probes" / "live_http_boundary_probe.py"
)
COUNTER_PROBE_PATH = (
    Path(__file__).parent / "_socket_guard_probes" / "http_counter_probe.py"
)


# --------------------------------------------------------------------------
# (a) the real network transports are blocked, sync and async
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_real_transport_is_blocked() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError, match="External HTTP is disabled"):
            await client.get(EXTERNAL_URL)


def test_sync_real_transport_is_blocked() -> None:
    with httpx.Client() as client:
        with pytest.raises(httpx.ConnectError, match="External HTTP is disabled"):
            client.get(EXTERNAL_URL)


def test_requests_adapter_is_blocked() -> None:
    with pytest.raises(requests.ConnectionError, match="External HTTP is disabled"):
        requests.get(EXTERNAL_URL, timeout=1)


def test_blocked_error_type_matches_a_real_connect_failure() -> None:
    """The boundary must not change which ``except`` clause callers land in.

    Providers here are wrapped in ``except httpx.HTTPError`` / retry / fail-open
    branches. A blocked connect surfaces as ``httpx.ConnectError``, so the
    boundary raises the same type rather than a bare ``RuntimeError`` that would
    escape those handlers and change behaviour.
    """

    assert issubclass(httpx.ConnectError, httpx.TransportError)
    assert issubclass(httpx.ConnectError, httpx.HTTPError)
    assert issubclass(requests.ConnectionError, requests.RequestException)


# --------------------------------------------------------------------------
# (b) in-process transports keep working
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_transport_is_untouched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"seen": str(request.url)})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(EXTERNAL_URL)

    assert response.status_code == 200
    assert response.json()["seen"] == EXTERNAL_URL


@pytest.mark.asyncio
async def test_asgi_transport_is_untouched() -> None:
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"asgi-ok"})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://rob1296-asgi.invalid"
    ) as client:
        response = await client.get("/probe")

    assert response.text == "asgi-ok"


def test_wsgi_transport_is_untouched() -> None:
    def app(environ, start_response):
        start_response("200 OK", [("content-type", "text/plain")])
        return [b"wsgi-ok"]

    transport = httpx.WSGITransport(app=app)
    with httpx.Client(
        transport=transport, base_url="https://rob1296-wsgi.invalid"
    ) as client:
        response = client.get("/probe")

    assert response.text == "wsgi-ok"


@pytest.mark.asyncio
async def test_custom_user_transport_is_untouched() -> None:
    class _CustomTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, text="custom-ok")

    async with httpx.AsyncClient(transport=_CustomTransport()) as client:
        response = await client.get(EXTERNAL_URL)

    assert response.status_code == 201
    assert response.text == "custom-ok"


# --------------------------------------------------------------------------
# (c) the opt-out reaches the real transport slot without opening the network
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opt_out_reaches_the_real_transport_slot(
    allow_external_http, monkeypatch
) -> None:
    """With the opt-out, a stand-in patched over the real transport is reached.

    This is the case the boundary would otherwise shadow: a boundary test that
    installs its own ``AsyncHTTPTransport`` stub. Proving the stub runs proves
    the fixture stepped aside.
    """

    _ = allow_external_http
    seen: list[str] = []

    async def _stand_in(self, request: httpx.Request, *args, **kwargs):
        seen.append(str(request.url))
        return httpx.Response(204, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _stand_in)

    async with httpx.AsyncClient() as client:
        response = await client.get(EXTERNAL_URL)

    assert response.status_code == 204
    assert seen == [EXTERNAL_URL]


def test_opt_out_does_not_open_the_real_network(allow_external_http) -> None:
    """Opting out of the boundary does not opt out of the socket guard."""

    _ = allow_external_http
    assert socket_guard.is_current_test_exempt() is False
    assert socket_guard.is_socket_address_permitted(("203.0.113.1", 443)) is False


# --------------------------------------------------------------------------
# (d) armed live keeps its intentional boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("has_live_marker", "run_live", "fixturenames", "expected"),
    [
        (False, False, (), True),
        (False, True, (), True),
        (True, False, (), True),
        (True, True, (), False),
        (False, False, ("allow_external_http",), False),
        (True, True, ("allow_external_http",), False),
    ],
)
def test_boundary_activation_truth_table(
    has_live_marker: bool,
    run_live: bool,
    fixturenames: tuple[str, ...],
    expected: bool,
) -> None:
    assert (
        boundary.boundary_is_active(
            has_live_marker=has_live_marker,
            run_live=run_live,
            fixturenames=fixturenames,
        )
        is expected
    )


def _nested_env() -> dict[str, str]:
    from tests.test_rob1296_live_only_socket_guard import _nested_pytest_environment

    return _nested_pytest_environment()


def test_armed_live_session_is_not_wrapped_by_the_boundary() -> None:
    """End to end: under ``--run-live`` the fixture leaves the transport alone.

    The probe asserts an identity, never a request, so this proves the opt-out
    without any possibility of live traffic.
    """

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PROBE_PATH),
            "--run-live",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_a_default_session_does_wrap_the_transport() -> None:
    """The mirror image: without ``--run-live`` the same probe is skipped."""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PROBE_PATH),
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped" in result.stdout


# --------------------------------------------------------------------------
# evidence counter
# --------------------------------------------------------------------------


def test_blocked_requests_are_counted_and_reported() -> None:
    before = boundary.snapshot()
    with httpx.Client() as client:
        with pytest.raises(httpx.ConnectError):
            client.get("https://rob1296-counter.invalid/probe")
    after = boundary.snapshot()

    host = "rob1296-counter.invalid"
    assert after.get(host, 0) == before.get(host, 0) + 1
    assert host in boundary.format_summary(after)


def test_summary_line_is_explicit_when_nothing_was_blocked() -> None:
    assert boundary.format_summary({}) == (
        "ROB-1296 external HTTP boundary: 0 blocked requests"
    )


def test_loopback_hosts_are_never_blocked() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        assert boundary.is_loopback_host(host) is True
    for host in ("openapi.koreainvestment.com", "api.upbit.com", "", None):
        assert boundary.is_loopback_host(host) is False


def test_reported_counter_survives_an_xdist_run(tmp_path: Path) -> None:
    """The controller must aggregate worker counts, not drop them.

    Driven by a dedicated probe that makes one intentional request to a reserved
    ``.invalid`` host. A real under-mocked test file must never be used here: the
    assertion would then only pass while that leak survives, turning a defect
    into a required baseline.
    """

    report_path = tmp_path / "guard.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(COUNTER_PROBE_PATH),
            "-n",
            "2",
            "--dist=loadfile",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "--socket-guard-report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(socket_guard.PROJECT_ROOT),
        env=_nested_env(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ROB-1296 external HTTP boundary: 1 blocked requests" in result.stdout
    assert "rob1296-counter-probe.invalid=1" in result.stdout

    # The probe was stopped above the socket layer, so the guard saw nothing.
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["active"] is True
    assert report["blocked_attempts"] == 0


# --------------------------------------------------------------------------
# curl_cffi (libcurl) — neither an httpx transport nor a requests adapter
# --------------------------------------------------------------------------


def test_curl_cffi_requests_are_blocked_and_counted() -> None:
    """The layer the other two could not see.

    ``curl_cffi`` binds libcurl, so it is not an httpx transport, not a
    ``requests`` adapter, and its ``connect(2)`` happens in C where the socket
    guard's monkeypatches cannot reach. yfinance uses it, which is how 29
    non-live tests were reaching query1.finance.yahoo.com for real while every
    counter read zero.
    """

    from curl_cffi.requests import Session

    host = "rob1296-curl.invalid"
    before = boundary.snapshot()
    with Session() as session:
        with pytest.raises(Exception, match="External HTTP is disabled"):
            session.get(f"https://{host}/probe", timeout=3)
    after = boundary.snapshot()

    assert after.get(host, 0) == before.get(host, 0) + 1


def test_curl_cffi_session_classes_are_discovered() -> None:
    """Both sync and async session classes must be intercepted."""

    names = {cls.__name__ for cls in boundary.curl_session_classes()}
    assert "Session" in names
    assert "AsyncSession" in names


def test_curl_cffi_loopback_is_not_blocked() -> None:
    """Local test servers must stay reachable through curl_cffi too."""

    from curl_cffi.requests import Session

    before = boundary.snapshot()
    with Session() as session:
        with contextlib.suppress(Exception):
            # Nothing is listening; what matters is that the guard did not
            # intercept it, so the counter must not move.
            session.get("http://127.0.0.1:1/probe", timeout=1)
    assert boundary.snapshot() == before
