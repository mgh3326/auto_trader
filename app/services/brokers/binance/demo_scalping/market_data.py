"""ROB-307 PR1 — read-only Demo-host market-data adapter.

Fetches klines + bookTicker over **unsigned** GETs from Demo hosts only:
``demo-api.binance.com`` (spot) and ``demo-fapi.binance.com`` (futures).
Both serve real public market data without credentials (verified
2026-05-24). A transport-layer host hook fails closed on any non-Demo
host, so the signal path can never reach ``api.binance.com`` — this is a
deliberate, self-contained allowlist (the live ``host_allowlist`` is
banned here by the import guard).

No signing, no credentials, no order endpoints reachable from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.services.brokers.binance.demo_scalping.contract import Product
from app.services.brokers.binance.demo_scalping.signal import Candle

DEMO_DATA_HOSTS: frozenset[str] = frozenset(
    {"demo-api.binance.com", "demo-fapi.binance.com"}
)

_BASE_URL: dict[str, str] = {
    "spot": "https://demo-api.binance.com",
    "usdm_futures": "https://demo-fapi.binance.com",
}
_KLINES_PATH: dict[str, str] = {
    "spot": "/api/v3/klines",
    "usdm_futures": "/fapi/v1/klines",
}
_BOOK_TICKER_PATH: dict[str, str] = {
    "spot": "/api/v3/ticker/bookTicker",
    "usdm_futures": "/fapi/v1/ticker/bookTicker",
}

_BPS = Decimal("10000")


class DemoDataHostBlocked(RuntimeError):
    """Raised when a request targets a host outside ``DEMO_DATA_HOSTS``."""


def assert_demo_data_host(host: str) -> None:
    """Strict equality match — no suffix/wildcard. Fails closed."""
    if host not in DEMO_DATA_HOSTS:
        raise DemoDataHostBlocked(
            f"Host {host!r} is not a Demo data host. "
            "Allowed: " + ", ".join(sorted(DEMO_DATA_HOSTS))
        )


@dataclass(frozen=True)
class BookTicker:
    bid: Decimal
    ask: Decimal


def spread_bps(book: BookTicker) -> Decimal:
    mid = (book.bid + book.ask) / Decimal("2")
    return (book.ask - book.bid) / mid * _BPS


def data_age_seconds(latest: Candle, *, now_ms: int) -> float:
    """Freshness of the latest candle, measured from its ``open_time``.

    Binance returns the in-progress candle (its ``close_time`` is in the
    future), so ``open_time`` is the correct anchor: ~0..interval for a
    live feed, growing once the feed stalls. Clamped at 0 against clock
    skew so the age is never negative.
    """
    return max(0.0, (now_ms - latest.open_time_ms) / 1000.0)


async def _enforce_demo_host(request: httpx.Request) -> None:
    assert_demo_data_host(request.url.host)


def build_demo_data_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        event_hooks={"request": [_enforce_demo_host]},
    )


class DemoScalpingMarketData:
    """Read-only Demo-host market-data reader (klines + bookTicker)."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or build_demo_data_client()
        self._owns_client = client is None

    async def fetch_klines(
        self,
        product: Product,
        symbol: str,
        *,
        interval: str = "1m",
        limit: int = 50,
    ) -> list[Candle]:
        url = _BASE_URL[product] + _KLINES_PATH[product]
        resp = await self._client.get(
            url, params={"symbol": symbol, "interval": interval, "limit": limit}
        )
        resp.raise_for_status()
        return [
            Candle(
                open_time_ms=int(row[0]),
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                close_time_ms=int(row[6]),
            )
            for row in resp.json()
        ]

    async def fetch_book_ticker(self, product: Product, symbol: str) -> BookTicker:
        url = _BASE_URL[product] + _BOOK_TICKER_PATH[product]
        resp = await self._client.get(url, params={"symbol": symbol})
        resp.raise_for_status()
        payload = resp.json()
        return BookTicker(
            bid=Decimal(str(payload["bidPrice"])),
            ask=Decimal(str(payload["askPrice"])),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
