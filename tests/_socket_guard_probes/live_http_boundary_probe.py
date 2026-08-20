"""Probe for the armed-live half of the ROB-1296 external-HTTP boundary.

Driven by tests/test_rob1296_external_http_boundary.py through a nested pytest
session, because "was the autouse fixture skipped?" can only be observed from
inside a session that actually has ``--run-live``. Deliberately not named
``test_*.py`` so the outer suite never collects it.

It asserts an *identity*, never a request: reaching the real transport would
still be refused by the socket guard, so nothing here can touch the network.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.live
def test_armed_live_keeps_the_real_transport() -> None:
    from tests import _external_http_boundary as boundary

    assert (
        httpx.AsyncHTTPTransport.handle_async_request.__qualname__
        != "_block_external_http_boundary.<locals>._blocked_async"
    )
    assert (
        boundary.boundary_is_active(
            has_live_marker=True, run_live=True, fixturenames=()
        )
        is False
    )
