"""Redis-cached Upbit recent trades fetcher."""

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
    TRADES_STALE_TOLERANCE_SECONDS,
    TRADES_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitTradesBlock,
    _now_utc,
)

logger = logging.getLogger(__name__)
TradesFetcher = Callable[[str, int], Awaitable[list[dict[str, Any]]]]
_NS = "upbit:public:read:trades:v1"
_MAX_COUNT = 500
_STATE_RANK = {"fresh": 0, "missing": 1, "stale": 2, "unavailable": 3}


def _key(market: str, count: int) -> str:
    return f"{_NS}:{market.upper()}:{count}"


class TradesCache:
    def __init__(self, *, redis, fetcher: TradesFetcher) -> None:
        self._redis = redis
        self._fetcher = fetcher

    async def get(self, market: str, count: int = 50) -> UpbitTradesBlock:
        market = str(market or "").strip().upper()
        count = max(1, min(int(count), _MAX_COUNT))
        if not market:
            return UpbitTradesBlock(
                meta=UpbitBlockMeta(
                    source="upbit_trades", state="missing", label="Upbit trades"
                )
            )
        key = _key(market, count)
        cached = await read_json(self._redis, key)
        now = _now_utc()
        if (
            cached is not None
            and (now - cached["cachedAt"]).total_seconds() <= TRADES_TTL_SECONDS
        ):
            return self._block(
                {market: cached["rows"]},
                state="fresh",
                fetched_at=cached["fetchedAt"],
                cached_at=cached["cachedAt"],
            )
        try:
            rows = await self._fetcher(market, count)
            await write_json(
                self._redis,
                key,
                {"rows": rows, "fetchedAt": now, "cachedAt": now},
                ex=TRADES_STALE_TOLERANCE_SECONDS,
            )
            return self._block(
                {market: list(rows)}, state="fresh", fetched_at=now, cached_at=now
            )
        except Exception as exc:  # noqa: BLE001
            reason = classify_error(exc)
            logger.warning("upbit_trades_cache fetch failed reason=%s", reason)
            if (
                cached is not None
                and (now - cached["cachedAt"]).total_seconds()
                <= TRADES_STALE_TOLERANCE_SECONDS
            ):
                return self._block(
                    {market: cached["rows"]},
                    state="stale",
                    fetched_at=cached["fetchedAt"],
                    cached_at=cached["cachedAt"],
                    error_reason=reason,
                )
            return UpbitTradesBlock(
                meta=UpbitBlockMeta(
                    source="upbit_trades",
                    state="unavailable",
                    label="Upbit trades",
                    errorReason=reason,
                )
            )

    @classmethod
    def merge(cls, blocks: list[UpbitTradesBlock]) -> UpbitTradesBlock:
        if not blocks:
            return UpbitTradesBlock(
                meta=UpbitBlockMeta(
                    source="upbit_trades", state="missing", label="Upbit trades"
                )
            )
        worst = max(blocks, key=lambda b: _STATE_RANK[b.meta.state])
        merged: dict[str, list[dict[str, Any]]] = {}
        for block in blocks:
            merged.update(block.trades)
        return UpbitTradesBlock(
            meta=UpbitBlockMeta(
                source="upbit_trades",
                state=worst.meta.state,
                label="Upbit trades",
                fetchedAt=worst.meta.fetchedAt,
                cachedAt=worst.meta.cachedAt,
                ttlSeconds=TRADES_TTL_SECONDS,
                errorReason=worst.meta.errorReason,
            ),
            trades=merged,
        )

    def _block(
        self,
        trades: dict[str, list[dict[str, Any]]],
        *,
        state: str,
        fetched_at: datetime,
        cached_at: datetime | None,
        error_reason: str | None = None,
    ) -> UpbitTradesBlock:
        return UpbitTradesBlock(
            meta=UpbitBlockMeta(
                source="upbit_trades",
                state=state,
                label="Upbit trades",
                fetchedAt=fetched_at,
                cachedAt=cached_at,
                ttlSeconds=TRADES_TTL_SECONDS,
                errorReason=error_reason,
            ),
            trades=trades,
        )
