"""ROB-1296 external-HTTP boundary: policy, counter, and reporting helpers.

The ROB-1880 socket guard already refuses these connections, so nothing here
decides *whether* traffic may leave — it cannot, either way. What this adds is a
clean failure and a visible counter. A guard refusal lands deep inside anyio, so
the caller sees an opaque ``ExceptionGroup`` and the attempt shows up only as a
number; blocking one layer up lets the same traffic fail as the exact exception
type a real blocked connect produces, and lets the evidence name the host.

Kept out of ``conftest.py`` so the policy is importable by the regression tests
that pin it.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable
from typing import Final

MESSAGE: Final = (
    "External HTTP is disabled during pytest (ROB-1296). Mock the client at its "
    "call site, or request allow_external_http for a boundary test."
)
OPT_OUT_FIXTURE: Final = "allow_external_http"

_LOCK = threading.Lock()
_BLOCKED_BY_HOST: Counter[str] = Counter()


def is_loopback_host(host: object) -> bool:
    """Reuse the socket guard's address policy so the two never disagree."""

    from tests._socket_guard import is_allowed_local_address

    if not isinstance(host, str) or not host:
        return False
    return is_allowed_local_address((host, 0))


def boundary_is_active(
    *, has_live_marker: bool, run_live: bool, fixturenames: Iterable[str]
) -> bool:
    """Return whether the boundary should be installed for one test item.

    Mirrors the socket guard's own exemption: an armed ``live`` item keeps its
    intentional network boundary, and a test that explicitly asks to drive the
    real transport opts out. Everything else is blocked.
    """

    if OPT_OUT_FIXTURE in set(fixturenames):
        return False
    return not (has_live_marker and run_live)


def record_block(host: str) -> None:
    with _LOCK:
        _BLOCKED_BY_HOST[host] += 1


def snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(sorted(_BLOCKED_BY_HOST.items()))


def reset() -> None:
    with _LOCK:
        _BLOCKED_BY_HOST.clear()


def format_summary(counts: dict[str, int]) -> str:
    if not counts:
        return "ROB-1296 external HTTP boundary: 0 blocked requests"
    hosts = " ".join(f"{host}={count}" for host, count in sorted(counts.items()))
    return (
        f"ROB-1296 external HTTP boundary: {sum(counts.values())} blocked "
        f"requests across {len(counts)} hosts -- {hosts}"
    )


def curl_session_classes() -> tuple[type, ...]:
    """Every ``curl_cffi`` session class whose ``request`` must be intercepted.

    Returned rather than hard-coded so an environment without ``curl_cffi``
    installed simply has nothing to patch, and so the async variant is covered
    when the installed version provides one.
    """

    try:
        from curl_cffi import requests as curl_requests
    except ImportError:  # pragma: no cover - curl_cffi is a hard dependency here
        return ()

    classes = []
    for name in ("Session", "AsyncSession"):
        candidate = getattr(curl_requests, name, None)
        if isinstance(candidate, type):
            classes.append(candidate)
    return tuple(classes)


def build_curl_request_blocker(session_class: type):
    """Wrap ``session_class.request`` so non-loopback hosts fail closed.

    Raises ``curl_cffi``'s own ``ConnectionError`` -- what an unreachable host
    already produces through this client -- so callers land in the same
    ``except`` branch they do today.
    """

    from urllib.parse import urlsplit

    from curl_cffi.requests import errors as curl_errors

    original = session_class.request

    def _blocked(self, method, url, *args, **kwargs):
        host = urlsplit(str(url)).hostname or ""
        if is_loopback_host(host):
            return original(self, method, url, *args, **kwargs)
        record_block(host)
        raise curl_errors.RequestsError(f"{MESSAGE} [{host}]")

    return _blocked
