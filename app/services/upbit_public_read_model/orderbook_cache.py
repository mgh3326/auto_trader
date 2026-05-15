"""Redis-cached Upbit orderbook fetcher with derived spread metadata."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from app.services.upbit_public_read_model.cache_common import (
    classify_error,
    read_json,
    write_json,
)
from app.services.upbit_public_read_model.types import (
    ORDERBOOK_STALE_TOLERANCE_SECONDS,
    ORDERBOOK_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitOrderbookBlock,
    _now_utc,
)

logger = logging.getLogger(__name__)
OrderbookFetcher = Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
_NS = "upbit:public:read:orderbook:v1"


def _key(market: str) -> str:
    return f"{_NS}:{market.upper()}"


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _spread_pct(book: dict[str, Any]) -> float | None:
    units = list(book.get("orderbook_units") or [])
    if not units:
        return None
    first_unit = units[0]
    ask = _float_or_none(first_unit.get("ask_price"))
    bid = _float_or_none(first_unit.get("bid_price"))
    if ask is None or bid is None or bid <= 0:
        return None
    return ((ask - bid) / bid) * 100


class OrderbookCache:
    def __init__(self, *, redis, fetcher: OrderbookFetcher) -> None:
        self._redis = redis
        self._fetcher = fetcher

    async def get(self, markets: list[str]) -> UpbitOrderbookBlock:
        markets = [str(m).upper() for m in markets if str(m or "").strip()]
        if not markets:
            return UpbitOrderbookBlock(
                meta=UpbitBlockMeta(
                    source="upbit_orderbook", state="missing", label="Upbit orderbook"
                )
            )
        now = _now_utc()
        cached_by_market = {m: await read_json(self._redis, _key(m)) for m in markets}
        fresh = {
            m: c["orderbook"]
            for m, c in cached_by_market.items()
            if c and (now - c["cachedAt"]).total_seconds() <= ORDERBOOK_TTL_SECONDS
        }
        missing = [m for m in markets if m not in fresh]
        books = dict(fresh)
        reason = None
        state = "fresh"
        if missing:
            try:
                fetched = await self._fetcher(missing)
                fetched = {str(k).upper(): v for k, v in fetched.items()}
                for market, book in fetched.items():
                    await write_json(
                        self._redis,
                        _key(market),
                        {"orderbook": book, "fetchedAt": now, "cachedAt": now},
                        ex=ORDERBOOK_STALE_TOLERANCE_SECONDS,
                    )
                books.update(fetched)
                unfetched = [m for m in missing if m not in fetched]
                if unfetched:
                    reason = "partial_missing"
                    stale = self._stale_books(cached_by_market, now, markets=unfetched)
                    books.update(stale)
                    state = "stale" if books else "unavailable"
            except Exception as exc:  # noqa: BLE001
                reason = classify_error(exc)
                logger.warning("upbit_orderbook_cache fetch failed reason=%s", reason)
                stale = self._stale_books(cached_by_market, now, markets=missing)
                books.update(stale)
                state = "stale" if books else "unavailable"
        fetched_at = now
        cached_at = now if books and state == "fresh" else None
        if state == "stale":
            cached_vals = [c for c in cached_by_market.values() if c]
            fetched_at = max((c["fetchedAt"] for c in cached_vals), default=now)
            cached_at = max((c["cachedAt"] for c in cached_vals), default=None)
        return self._block(
            books,
            state=state,
            fetched_at=fetched_at if books else None,
            cached_at=cached_at,
            error_reason=reason,
        )

    @staticmethod
    def _stale_books(
        cached_by_market: dict[str, dict[str, Any] | None],
        now: datetime,
        *,
        markets: list[str],
    ) -> dict[str, dict[str, Any]]:
        requested = set(markets)
        return {
            market: cached["orderbook"]
            for market, cached in cached_by_market.items()
            if market in requested
            and cached
            and (now - cached["cachedAt"]).total_seconds()
            <= ORDERBOOK_STALE_TOLERANCE_SECONDS
        }

    def _block(
        self,
        books: dict[str, dict[str, Any]],
        *,
        state: str,
        fetched_at: datetime | None,
        cached_at: datetime | None,
        error_reason: str | None = None,
    ) -> UpbitOrderbookBlock:
        return UpbitOrderbookBlock(
            meta=UpbitBlockMeta(
                source="upbit_orderbook",
                state=state,
                label="Upbit orderbook",
                fetchedAt=fetched_at,
                cachedAt=cached_at,
                ttlSeconds=ORDERBOOK_TTL_SECONDS,
                errorReason=error_reason,
            ),
            orderbooks=books,
            spreadsPct={m: _spread_pct(b) for m, b in books.items()},
        )
