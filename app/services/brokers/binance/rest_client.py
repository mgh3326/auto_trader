"""ROB-285 — Binance public REST client.

Public endpoints only: ``exchangeInfo``, ``klines``, ``bookTicker``. No
signed endpoints. No API key required. Host allowlist enforced by the
transport layer (``app/services/brokers/binance/transport.py``).

Rate-limit handling (Task 7):
- Soft-throttle: when ``X-MBX-USED-WEIGHT-1M / declared_weight_limit >= 0.8``,
  the next call sleeps for the remainder of the current minute.
- Hard-stop: 429/418 responses raise ``BinanceRateLimited`` with
  ``Retry-After`` seconds attached. No automatic retry — the caller decides.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from decimal import Decimal
from typing import Any, Final

import httpx

from app.services.brokers.binance.dto import (
    BinanceBookTicker,
    BinanceExchangeSymbolInfo,
    BinanceKlineRow,
)
from app.services.brokers.binance.errors import BinanceRateLimited
from app.services.brokers.binance.rate_limit_telemetry import (
    emit_rate_limit_snapshot,
    parse_rate_limit_headers,
)
from app.services.brokers.binance.transport import build_public_client

_BASE_URL: Final[str] = "https://api.binance.com"
_SOFT_THROTTLE_THRESHOLD: Final[float] = 0.8
_DEFAULT_DECLARED_WEIGHT: Final[int] = 1200

logger = logging.getLogger("app.services.brokers.binance.rest")


def _kline_from_row(
    symbol: str,
    interval: str,
    row: list[Any],
    *,
    now: dt.datetime | None = None,
) -> BinanceKlineRow:
    """Translate a raw Binance kline list into a typed DTO.

    A REST kline is considered closed when ``close_time < now``. The WS
    ``x`` flag is used by the WS client; here we infer ``is_closed``
    from time ordering. ``now`` is injectable for tests.
    """
    open_time = dt.datetime.fromtimestamp(row[0] / 1000.0, tz=dt.UTC)
    close_time = dt.datetime.fromtimestamp(row[6] / 1000.0, tz=dt.UTC)
    current = now or dt.datetime.now(tz=dt.UTC)
    return BinanceKlineRow(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        base_volume=Decimal(row[5]),
        quote_volume=Decimal(row[7]) if row[7] is not None else None,
        trade_count=int(row[8]) if row[8] is not None else None,
        taker_buy_base_volume=Decimal(row[9]) if row[9] is not None else None,
        taker_buy_quote_volume=Decimal(row[10]) if row[10] is not None else None,
        is_closed=close_time < current,
    )


class BinancePublicRestClient:
    """Read-only REST client for Binance public endpoints.

    Intentionally NOT exposed: ``account()``, ``order()``, ``open_orders()``,
    ``my_trades()``, ``cancel_order()``, ``user_data_stream()`` — these
    belong to signed-endpoint surface owned by Child C.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        declared_weight_limit: int = _DEFAULT_DECLARED_WEIGHT,
    ) -> None:
        self._client = client or build_public_client()
        self._owns_client = client is None
        self._declared_weight_limit = declared_weight_limit
        self._last_used_weight: int | None = None

    async def __aenter__(self) -> "BinancePublicRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _maybe_soft_throttle(self) -> None:
        if self._last_used_weight is None:
            return
        ratio = self._last_used_weight / self._declared_weight_limit
        if ratio >= _SOFT_THROTTLE_THRESHOLD:
            # Sleep to the next minute window. Binance counters reset at
            # the minute boundary; sleeping the remainder of the current
            # minute is the simplest safe behavior.
            now = dt.datetime.now(tz=dt.UTC)
            sleep_seconds = max(60.0 - now.second, 1.0)
            logger.warning(
                "binance.rate_limit soft-throttling: used_weight=%s "
                "declared=%s sleeping=%.1fs",
                self._last_used_weight,
                self._declared_weight_limit,
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

    async def _send(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request with rate-limit observation + soft/hard stops.

        - Soft-throttle: sleep before the request when last_used_weight
          ratio is at or above ``_SOFT_THROTTLE_THRESHOLD``.
        - Hard-stop: raise ``BinanceRateLimited`` on 429/418.
        """
        await self._maybe_soft_throttle()
        resp = await self._client.request(method, url, **kwargs)
        snap = parse_rate_limit_headers(dict(resp.headers))
        emit_rate_limit_snapshot(
            snap, declared_weight_limit=self._declared_weight_limit
        )
        if snap.used_weight_1m is not None:
            self._last_used_weight = snap.used_weight_1m
        if resp.status_code in (418, 429):
            retry_after = float(resp.headers.get("retry-after", "60"))
            raise BinanceRateLimited(
                retry_after,
                f"Binance {resp.status_code}; Retry-After {retry_after}s",
            )
        return resp

    async def exchange_info(self, symbol: str) -> BinanceExchangeSymbolInfo:
        resp = await self._send(
            "GET",
            f"{_BASE_URL}/api/v3/exchangeInfo",
            params={"symbol": symbol},
        )
        resp.raise_for_status()
        payload = resp.json()
        sym = payload["symbols"][0]
        return BinanceExchangeSymbolInfo(
            symbol=sym["symbol"],
            base_asset=sym["baseAsset"],
            quote_asset=sym["quoteAsset"],
            status=sym["status"],
        )

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
        limit: int = 500,
    ) -> list[BinanceKlineRow]:
        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time is not None:
            params["startTime"] = int(start_time.timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(end_time.timestamp() * 1000)
        resp = await self._send("GET", f"{_BASE_URL}/api/v3/klines", params=params)
        resp.raise_for_status()
        rows = resp.json()
        return [_kline_from_row(symbol, interval, r) for r in rows]

    async def book_ticker(self, symbol: str) -> BinanceBookTicker:
        resp = await self._send(
            "GET",
            f"{_BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
        )
        resp.raise_for_status()
        data = resp.json()
        return BinanceBookTicker(
            symbol=data["symbol"],
            bid_price=Decimal(data["bidPrice"]),
            bid_qty=Decimal(data["bidQty"]),
            ask_price=Decimal(data["askPrice"]),
            ask_qty=Decimal(data["askQty"]),
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )
