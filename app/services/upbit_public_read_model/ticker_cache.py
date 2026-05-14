"""Redis-cached Upbit ticker fetcher with freshness/error envelope."""

from __future__ import annotations

import hashlib
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
    TICKER_STALE_TOLERANCE_SECONDS,
    TICKER_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitTickerBlock,
    _now_utc,
)

logger = logging.getLogger(__name__)
TickerFetcher = Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
_NS = "upbit:public:read:ticker:v1"


def _key(markets: list[str]) -> str:
    digest = hashlib.sha1(
        ",".join(sorted({m.upper() for m in markets})).encode("utf-8")
    ).hexdigest()
    return f"{_NS}:{digest}"


class TickerCache:
    def __init__(self, *, redis, fetcher: TickerFetcher) -> None:
        self._redis = redis
        self._fetcher = fetcher

    async def get(self, markets: list[str]) -> UpbitTickerBlock:
        markets = [str(m).upper() for m in markets if str(m or "").strip()]
        if not markets:
            return UpbitTickerBlock(
                meta=UpbitBlockMeta(
                    source="upbit_ticker", state="missing", label="Upbit ticker"
                )
            )
        key = _key(markets)
        cached = await read_json(self._redis, key)
        now = _now_utc()
        if (
            cached is not None
            and (now - cached["cachedAt"]).total_seconds() <= TICKER_TTL_SECONDS
        ):
            return self._block(
                cached["tickers"],
                state="fresh",
                fetched_at=cached["fetchedAt"],
                cached_at=cached["cachedAt"],
            )
        try:
            rows = await self._fetcher(markets)
            tickers = {
                str(r.get("market") or "").upper(): r for r in rows if r.get("market")
            }
            await write_json(
                self._redis,
                key,
                {"tickers": tickers, "fetchedAt": now, "cachedAt": now},
                ex=TICKER_STALE_TOLERANCE_SECONDS,
            )
            return self._block(tickers, state="fresh", fetched_at=now, cached_at=now)
        except Exception as exc:  # noqa: BLE001
            reason = classify_error(exc)
            logger.warning("upbit_ticker_cache fetch failed reason=%s", reason)
            if (
                cached is not None
                and (now - cached["cachedAt"]).total_seconds()
                <= TICKER_STALE_TOLERANCE_SECONDS
            ):
                return self._block(
                    cached["tickers"],
                    state="stale",
                    fetched_at=cached["fetchedAt"],
                    cached_at=cached["cachedAt"],
                    error_reason=reason,
                )
            return UpbitTickerBlock(
                meta=UpbitBlockMeta(
                    source="upbit_ticker",
                    state="unavailable",
                    label="Upbit ticker",
                    errorReason=reason,
                )
            )

    def _block(
        self,
        tickers: dict[str, dict[str, Any]],
        *,
        state: str,
        fetched_at: datetime,
        cached_at: datetime | None,
        error_reason: str | None = None,
    ) -> UpbitTickerBlock:
        return UpbitTickerBlock(
            meta=UpbitBlockMeta(
                source="upbit_ticker",
                state=state,
                label="Upbit ticker",
                fetchedAt=fetched_at,
                cachedAt=cached_at,
                ttlSeconds=TICKER_TTL_SECONDS,
                errorReason=error_reason,
            ),
            tickers=tickers,
        )
