from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.market_data.contracts import Quote
from app.services.market_quote_snapshots.repository import MarketQuoteSnapshotUpsert

logger = logging.getLogger(__name__)
QuoteFetcher = Callable[[str, str], Awaitable[Quote]]
_SENSITIVE_KEY_FRAGMENTS = ("key", "secret", "token", "password", "authorization")


@dataclass(frozen=True)
class MarketQuoteBuildResult:
    payloads: tuple[MarketQuoteSnapshotUpsert, ...]
    warnings: tuple[str, ...] = ()


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def redact_sensitive_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if any(fragment in key.lower() for fragment in _SENSITIVE_KEY_FRAGMENTS):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


async def default_quote_fetcher(symbol: str, market: str) -> Quote:
    from app.services.market_data.service import get_quote

    return await get_quote(symbol, market)


def _source_for_market(market: str, quote: Quote) -> str:
    source = (quote.source or "").strip().lower()
    if source:
        return source
    return {"kr": "kis", "us": "yahoo", "crypto": "upbit"}[market]


def _payload_from_quote(
    *, market: str, quote: Quote, snapshot_at: dt.datetime
) -> MarketQuoteSnapshotUpsert | None:
    price = _to_decimal(quote.price)
    if price is None:
        return None
    return MarketQuoteSnapshotUpsert(
        market=market,
        symbol=quote.symbol,
        source=_source_for_market(market, quote),
        snapshot_at=snapshot_at.replace(microsecond=0),
        price=price,
        previous_close=_to_decimal(quote.previous_close),
        open=_to_decimal(quote.open),
        high=_to_decimal(quote.high),
        low=_to_decimal(quote.low),
        volume=quote.volume,
        raw_payload=redact_sensitive_payload(
            {
                "source": quote.source,
                "value": quote.value,
            }
        ),
    )


async def build_quote_snapshots_for_market(
    *,
    market: str,
    symbols: Iterable[str],
    now: dt.datetime | None = None,
    concurrency: int = 4,
    fetcher: QuoteFetcher | None = None,
) -> MarketQuoteBuildResult:
    market_norm = market.strip().lower()
    fetch = fetcher or default_quote_fetcher
    snapshot_at = (
        (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(microsecond=0)
    )
    sem = asyncio.Semaphore(max(1, concurrency))
    symbols_list = [symbol.strip().upper() for symbol in symbols if symbol.strip()]
    payloads: list[MarketQuoteSnapshotUpsert | None] = [None] * len(symbols_list)
    warnings: list[str] = []

    async def _one(idx: int, symbol: str) -> None:
        async with sem:
            try:
                quote = await fetch(symbol, market_norm)
                payload = _payload_from_quote(
                    market=market_norm, quote=quote, snapshot_at=snapshot_at
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "quote snapshot fetch failed market=%s symbol=%s: %s",
                    market_norm,
                    symbol,
                    exc,
                )
                warnings.append(f"{symbol}: fetch failed ({exc})")
                return
            if payload is None:
                warnings.append(f"{symbol}: skipped because quote price is unavailable")
                return
            payloads[idx] = payload

    await asyncio.gather(
        *(_one(idx, symbol) for idx, symbol in enumerate(symbols_list))
    )
    return MarketQuoteBuildResult(
        payloads=tuple(payload for payload in payloads if payload is not None),
        warnings=tuple(warnings),
    )
