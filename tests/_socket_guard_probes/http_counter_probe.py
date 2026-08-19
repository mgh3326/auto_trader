"""Intentional single external request, used to verify the boundary counter.

Driven by tests/test_rob1296_external_http_boundary.py through a nested xdist
session. Deliberately not named ``test_*.py`` so the outer suite never collects
it.

This exists so the counter's worker->controller aggregation can be verified
against traffic this probe *creates on purpose*, rather than against a real
under-mocked provider call. Pinning a genuine leak as the fixture for a
mechanics test would make "the leak still exists" a passing condition, which is
the opposite of what ROB-1296 is for.

The host is an RFC 6761 reserved ``.invalid`` name, so even with every guard
removed there is nothing to connect to.
"""

from __future__ import annotations

import httpx

PROBE_HOST = "rob1296-counter-probe.invalid"
PROBE_URL = f"https://{PROBE_HOST}/aggregate"


def test_boundary_blocks_one_intentional_external_request() -> None:
    from tests import _socket_guard as socket_guard

    before = socket_guard.summary()["blocked_attempts"]

    with httpx.Client() as client:
        try:
            client.get(PROBE_URL)
        except httpx.ConnectError as error:
            assert "External HTTP is disabled" in str(error)
        else:  # pragma: no cover - the boundary must not let this through
            raise AssertionError("boundary did not block the probe request")

    # The request must be stopped at the HTTP boundary, above the socket layer:
    # the guard's blocked-attempt counter must not move.
    assert socket_guard.summary()["blocked_attempts"] == before
