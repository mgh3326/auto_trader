"""ROB-285 — Binance public REST client.

Public endpoints only: ``exchangeInfo``, ``klines``, ``bookTicker``. No
signed endpoints. No API key required. Host allowlist enforced by the
transport layer (``app/services/brokers/binance/transport.py``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Final

import httpx

from app.services.brokers.binance.dto import (
    BinanceBookTicker,
    BinanceExchangeSymbolInfo,
    BinanceKlineRow,
)
from app.services.brokers.binance.rate_limit_telemetry import (
    emit_rate_limit_snapshot,
    parse_rate_limit_headers,
)
from app.services.brokers.binance.transport import build_public_client

_BASE_URL: Final[str] = "https://api.binance.com"


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

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or build_public_client()
        self._owns_client = client is None

    async def __aenter__(self) -> "BinancePublicRestClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def exchange_info(self, symbol: str) -> BinanceExchangeSymbolInfo:
        resp = await self._client.get(
            f"{_BASE_URL}/api/v3/exchangeInfo",
            params={"symbol": symbol},
        )
        resp.raise_for_status()
        emit_rate_limit_snapshot(parse_rate_limit_headers(dict(resp.headers)))
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
        resp = await self._client.get(f"{_BASE_URL}/api/v3/klines", params=params)
        resp.raise_for_status()
        emit_rate_limit_snapshot(parse_rate_limit_headers(dict(resp.headers)))
        rows = resp.json()
        return [_kline_from_row(symbol, interval, r) for r in rows]

    async def book_ticker(self, symbol: str) -> BinanceBookTicker:
        resp = await self._client.get(
            f"{_BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
        )
        resp.raise_for_status()
        emit_rate_limit_snapshot(parse_rate_limit_headers(dict(resp.headers)))
        data = resp.json()
        return BinanceBookTicker(
            symbol=data["symbol"],
            bid_price=Decimal(data["bidPrice"]),
            bid_qty=Decimal(data["bidQty"]),
            ask_price=Decimal(data["askPrice"]),
            ask_qty=Decimal(data["askQty"]),
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )
