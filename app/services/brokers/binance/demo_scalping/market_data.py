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

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.services.brokers.binance.demo_scalping.contract import (
    MarketConditions,
    Product,
)
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


class MarketConditionsUnavailable(RuntimeError):
    """The server could not derive a trustworthy market snapshot.

    Raised on provider failure, an empty/malformed kline, a missing/invalid
    kline timestamp, or an invalid bid/ask quote. Callers MUST fail closed —
    no broker submit, no ledger read/write — rather than synthesize a 0/0
    snapshot that would silently disarm the spread/staleness gates (ROB-841).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _default_clock_ms() -> int:
    """Wall-clock epoch milliseconds. Injectable so tests can drive
    fetch-latency scenarios deterministically."""
    return int(time.time() * 1000)


def _validate_quote(book: BookTicker) -> None:
    """Reject non-finite (NaN / ±Inf), non-positive, or crossed (ask < bid)
    quotes as unavailable. The finiteness check runs first: comparing a
    ``Decimal('NaN')`` with ``<=`` raises ``InvalidOperation`` (which would
    otherwise leak as a generic error), and a ``Decimal('Infinity')`` ask
    slips past ``ask < bid`` yet poisons ``spread_bps`` with ``NaN`` — so a
    non-finite value must never reach the risk gates."""
    if not (book.bid.is_finite() and book.ask.is_finite()):
        raise MarketConditionsUnavailable(
            f"non_finite_quote: bid={book.bid} ask={book.ask}"
        )
    if book.bid <= 0 or book.ask <= 0 or book.ask < book.bid:
        raise MarketConditionsUnavailable(
            f"invalid_quote: bid={book.bid} ask={book.ask}"
        )


async def build_market_conditions(
    market_data: DemoScalpingMarketData,
    *,
    product: Product,
    symbol: str,
    clock_ms: Callable[[], int] = _default_clock_ms,
    spot_free_base_qty: Decimal = Decimal("0"),
) -> MarketConditions:
    """Derive a *server-observed* :class:`MarketConditions` from the Demo-host
    bookTicker + latest 1m kline (ROB-841).

    The ``spread_bps`` and ``data_age_seconds`` fields are always measured from
    the exchange's own quote/kline — never supplied or influenced by the caller.
    Fails closed via :class:`MarketConditionsUnavailable` on any provider error,
    empty/malformed kline, missing/invalid timestamp, or invalid/non-finite
    quote, so the caller can reject the order without touching broker or ledger.

    Data age is measured from a clock sampled **after both observations
    complete** (``clock_ms``, injectable), so real bookTicker + kline fetch
    latency is counted toward staleness — a pre-fetch clock could under-count
    the age by seconds and slip a stale feed past the ``stale_data`` gate.

    ``spot_free_base_qty`` is passed through for the spot long-only SELL gate;
    it is irrelevant to (and unused by) the futures path.
    """
    try:
        book = await market_data.fetch_book_ticker(product, symbol)
        candles = await market_data.fetch_klines(
            product, symbol, interval="1m", limit=1
        )
    except MarketConditionsUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — any provider/parse error fails closed
        raise MarketConditionsUnavailable(
            f"provider_error: {type(exc).__name__}: {exc}"
        ) from exc

    # Sample the clock only after BOTH observations complete so fetch latency
    # is counted toward the kline's age (see docstring).
    observed_ms = clock_ms()

    if not candles:
        raise MarketConditionsUnavailable("empty_kline")
    latest = candles[-1]
    if latest.open_time_ms is None or latest.open_time_ms <= 0:
        raise MarketConditionsUnavailable("missing_kline_timestamp")
    _validate_quote(book)

    return MarketConditions(
        spread_bps=spread_bps(book),
        data_age_seconds=data_age_seconds(latest, now_ms=observed_ms),
        spot_free_base_qty=spot_free_base_qty,
    )


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
