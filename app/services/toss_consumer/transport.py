from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

import httpx

from app.services.brokers.toss.errors import TossHostBlocked

TOSS_CONSUMER_HOSTS: Final[frozenset[str]] = frozenset({"wts-info-api.tossinvest.com"})
DEFAULT_TOSS_CONSUMER_BASE_URL: Final[str] = "https://wts-info-api.tossinvest.com"
DEFAULT_TOSS_CONSUMER_TIMEOUT: Final[float] = 10.0


def assert_toss_consumer_host(host: str | None, *, scheme: str | None = None) -> None:
    if scheme is not None and scheme != "https":
        raise TossHostBlocked(
            f"Scheme {scheme!r} is not allowed for Toss Consumer API; https is required."
        )
    if host not in TOSS_CONSUMER_HOSTS:
        raise TossHostBlocked(
            f"Host {host!r} is not in TOSS_CONSUMER_HOSTS. "
            "Allowed: " + ", ".join(sorted(TOSS_CONSUMER_HOSTS))
        )


def _assert_base_url_is_toss_consumer(base_url: str) -> None:
    parsed = urlsplit(base_url)
    assert_toss_consumer_host(parsed.hostname, scheme=parsed.scheme)


async def _on_request(request: httpx.Request) -> None:
    assert_toss_consumer_host(request.url.host, scheme=request.url.scheme)


async def _on_response(response: httpx.Response) -> None:
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise TossHostBlocked(
            f"Unexpected redirect from {response.request.url} to {location!r}; "
            "Toss Consumer API endpoints do not legitimately redirect. Refusing."
        )


def build_toss_consumer_client(
    *,
    base_url: str = DEFAULT_TOSS_CONSUMER_BASE_URL,
    timeout: float = DEFAULT_TOSS_CONSUMER_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    _assert_base_url_is_toss_consumer(base_url)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        follow_redirects=False,
        transport=transport,
        event_hooks={"request": [_on_request], "response": [_on_response]},
    )
