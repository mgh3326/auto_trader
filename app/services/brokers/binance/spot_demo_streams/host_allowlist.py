"""Spot Demo STREAM host allowlist (hk 6067, operator decision D-1).

Separate from ``spot_demo.host_allowlist.SPOT_DEMO_HOSTS`` (signed REST) and
from ``host_allowlist.PUBLIC_HOSTS`` (live public data). The two hosts below
are read-only stream endpoints:

  * ``demo-stream.binance.com``  — market streams (``/ws``, ``/stream``)
  * ``demo-ws-api.binance.com``  — WebSocket API (``/ws-api/v3``), used only to
    subscribe to the user data stream

Doc sources: https://developers.binance.com/en/docs/products/spot/demo-mode/general-info
and https://developers.binance.com/en/docs/products/spot/web-socket-api .

The check runs on the URL string BEFORE any socket is opened. Matching is
strict equality on the parsed hostname (no suffix match), the scheme must be
``wss`` and userinfo is refused.
"""

from __future__ import annotations

from typing import Final, Literal
from urllib.parse import urlsplit

from app.services.brokers.binance.errors import BinanceLiveHostBlocked

StreamKind = Literal["market", "user"]

SPOT_DEMO_MARKET_STREAM_HOST: Final[str] = "demo-stream.binance.com"
SPOT_DEMO_WS_API_HOST: Final[str] = "demo-ws-api.binance.com"

SPOT_DEMO_STREAM_HOSTS: Final[frozenset[str]] = frozenset(
    {SPOT_DEMO_MARKET_STREAM_HOST, SPOT_DEMO_WS_API_HOST}
)

_HOST_BY_KIND: Final[dict[str, str]] = {
    "market": SPOT_DEMO_MARKET_STREAM_HOST,
    "user": SPOT_DEMO_WS_API_HOST,
}
_PATHS_BY_KIND: Final[dict[str, frozenset[str]]] = {
    "market": frozenset({"/ws", "/stream"}),
    "user": frozenset({"/ws-api/v3"}),
}
_ALLOWED_PORTS: Final[frozenset[int]] = frozenset({443, 9443})

DEFAULT_MARKET_STREAM_URL_BASE: Final[str] = (
    f"wss://{SPOT_DEMO_MARKET_STREAM_HOST}:9443/stream"
)
DEFAULT_WS_API_URL: Final[str] = f"wss://{SPOT_DEMO_WS_API_HOST}/ws-api/v3"

# Defense-in-depth deny list: hosts a misconfiguration could point at. They
# are refused by the generic allowlist anyway; listing them gives a specific
# message and lets tests pin "mainnet/testnet never connect".
DENIED_STREAM_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "stream.binance.com",
        "data-stream.binance.vision",
        "ws-api.binance.com",
        "fstream.binance.com",
        "stream.testnet.binance.vision",
        "ws-api.testnet.binance.vision",
        "demo-fstream.binance.com",
        "demo-api.binance.com",
        "demo-fapi.binance.com",
        "api.binance.com",
    }
)


def assert_spot_demo_stream_url(url: str, *, kind: StreamKind) -> None:
    """Raise ``BinanceLiveHostBlocked`` unless ``url`` is the Spot Demo stream
    endpoint for ``kind``. Pure: opens nothing."""
    if kind not in _HOST_BY_KIND:
        raise BinanceLiveHostBlocked(f"unknown stream kind {kind!r}")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError as exc:
        raise BinanceLiveHostBlocked("malformed stream URL") from exc
    if host in DENIED_STREAM_HOSTS:
        raise BinanceLiveHostBlocked(
            f"Stream host {host!r} is a live/testnet/other-lane host; the Spot "
            "Demo collector connects only to its own demo stream hosts."
        )
    if host != _HOST_BY_KIND[kind]:
        raise BinanceLiveHostBlocked(
            f"Stream host {host!r} is not the Spot Demo {kind} stream host "
            f"{_HOST_BY_KIND[kind]!r}."
        )
    if parts.scheme != "wss":
        raise BinanceLiveHostBlocked("Spot Demo streams require the wss scheme")
    if parts.username is not None or parts.password is not None:
        raise BinanceLiveHostBlocked("userinfo is not allowed in a stream URL")
    if port is not None and port not in _ALLOWED_PORTS:
        raise BinanceLiveHostBlocked(f"port {port} is not an allowed stream port")
    if parts.path not in _PATHS_BY_KIND[kind]:
        raise BinanceLiveHostBlocked(
            f"path {parts.path!r} is not an allowed {kind} stream path"
        )
    if parts.fragment:
        raise BinanceLiveHostBlocked("fragment is not allowed in a stream URL")
