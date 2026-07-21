"""ROB-993 — real-time 1m -> 4h bar aggregation for the strategy loop.

Fetches 1m klines from the Futures Demo host (public, unsigned market
data — the same real market data Binance serves without credentials) and
hands them to the H1 offline builder
(``research.nautilus_scalping.rob974_features``) for 4h aggregation, so
the live loop and the offline research/backtest pipeline share the exact
same semantics: UTC-aligned 4h buckets, complete-only (a bucket missing
any of its 240 constituent 1m rows is never emitted — no forward-fill),
NO_SIGNAL is simply "no bar", never a synthesized/partial one.

Demo-host only (``demo-fapi.binance.com``) — enforced at the transport
layer via ``assert_futures_demo_host``, matching every other Futures Demo
reader in this codebase. Never reaches live ``fapi.binance.com``.
"""

from __future__ import annotations

import time

import httpx

from app.services.brokers.binance.futures_demo.host_allowlist import (
    assert_futures_demo_host,
)
from research.nautilus_scalping.rob974_features import (
    FOUR_HOUR_MS,
    MINUTE_MS,
    Bar4h,
    MinuteBar,
    build_complete_4h,
)

__all__ = [
    "FOUR_HOUR_MS",
    "MINUTE_MS",
    "Bar4h",
    "MinuteBar",
    "build_complete_4h",
    "build_bars_client",
    "fetch_1m_minute_bars",
    "latest_closed_bar",
]

_DEFAULT_BASE_URL = "https://demo-fapi.binance.com"
_KLINES_PATH = "/fapi/v1/klines"


async def _enforce_futures_demo_host(request: httpx.Request) -> None:
    assert_futures_demo_host(request.url.host)


def build_bars_client(*, base_url: str = _DEFAULT_BASE_URL) -> httpx.AsyncClient:
    """Unsigned httpx client pinned to the Futures Demo host.

    Host is validated both up front (construction) and on every request
    (event hook) — a misconfigured ``base_url`` fails closed before any
    HTTP is dispatched, and a redirect can never smuggle the client onto
    a non-Demo host mid-session.
    """
    assert_futures_demo_host(httpx.URL(base_url).host)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(10.0),
        event_hooks={"request": [_enforce_futures_demo_host]},
    )


async def fetch_1m_minute_bars(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    limit: int = 500,
) -> tuple[MinuteBar, ...]:
    """Fetch the latest closed 1m klines for ``symbol`` as :class:`MinuteBar`.

    Binance's kline response includes the in-progress candle as its last
    row (``closeTime`` in the future); that row is dropped here so
    ``build_complete_4h`` never sees a partial minute — H1's "no
    forward-fill, complete-only" contract starts at the minute layer, not
    just the 4h layer.
    """
    resp = await client.get(
        _KLINES_PATH, params={"symbol": symbol, "interval": "1m", "limit": limit}
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return ()
    now_ms = int(time.time() * 1000)
    bars: list[MinuteBar] = []
    for row in rows:
        close_time_ms = int(row[6])
        if close_time_ms > now_ms:
            continue  # in-progress candle — not yet closed
        bars.append(
            MinuteBar(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return tuple(bars)


def latest_closed_bar(bars: tuple[Bar4h, ...]) -> Bar4h | None:
    return bars[-1] if bars else None
