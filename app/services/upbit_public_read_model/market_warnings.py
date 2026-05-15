"""Read-only Upbit market-warning read model."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.upbit_symbol_universe import UpbitSymbolUniverse
from app.services.upbit_public_read_model.cache_common import (
    classify_error,
    read_json,
    write_json,
)
from app.services.upbit_public_read_model.types import (
    WARNINGS_STALE_TOLERANCE_SECONDS,
    WARNINGS_TTL_SECONDS,
    UpbitBlockMeta,
    UpbitMarketWarningEntry,
    UpbitMarketWarningsBlock,
    _now_utc,
)

_UPBIT_MARKET_ALL_URL = "https://api.upbit.com/v1/market/all"
_WARNINGS_KEY = "upbit:public:read:warnings:v1"


class MarketWarningsProvider(Protocol):
    def __call__(
        self, markets: list[str] | None = None
    ) -> Awaitable[UpbitMarketWarningsBlock]: ...


async def fetch_market_event_details() -> list[dict[str, Any]]:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(_UPBIT_MARKET_ALL_URL, params={"isDetails": "true"})
        response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("unexpected Upbit market/all response shape")
    return payload


class MarketWarningsService:
    def __init__(
        self,
        *,
        redis=None,
        db_session_factory=AsyncSessionLocal,
        detail_fetcher=fetch_market_event_details,
    ) -> None:
        self._redis = redis
        self._db_session_factory = db_session_factory
        self._detail_fetcher = detail_fetcher

    async def get(
        self,
        markets: list[str] | None = None,
        *,
        include_event_detail: bool = False,
        db: AsyncSession | None = None,
    ) -> UpbitMarketWarningsBlock:
        requested = {m.upper() for m in markets} if markets else None

        async def load(session: AsyncSession) -> list[UpbitSymbolUniverse]:
            stmt = select(UpbitSymbolUniverse).where(
                UpbitSymbolUniverse.quote_currency == "KRW",
                UpbitSymbolUniverse.is_active.is_(True),
            )
            if requested:
                stmt = stmt.where(UpbitSymbolUniverse.market.in_(sorted(requested)))
            result = await session.execute(
                stmt.order_by(UpbitSymbolUniverse.market.asc())
            )
            return list(result.scalars().all())

        if db is None:
            async with self._db_session_factory() as session:
                rows = await load(session)
        else:
            rows = await load(db)
        latest = max((getattr(row, "updated_at", None) for row in rows), default=None)
        entries = {
            row.market.upper(): UpbitMarketWarningEntry(
                market=row.market.upper(), warning=(row.market_warning or "NONE")
            )
            for row in rows
        }
        state = "fresh" if entries else "missing"
        label = "Upbit market warnings (universe)"
        error_reason = None
        if include_event_detail and entries:
            try:
                details = await self._get_detail_rows()
                by_market = {
                    str(item.get("market") or "").upper(): item for item in details
                }
                for market, entry in list(entries.items()):
                    event = by_market.get(market, {}).get("market_event")
                    entries[market] = entry.model_copy(update={"event": event})
            except Exception as exc:  # noqa: BLE001
                state = "stale" if entries else "unavailable"
                error_reason = classify_error(exc)
        return UpbitMarketWarningsBlock(
            meta=UpbitBlockMeta(
                source="upbit_market_warnings",
                state=state,
                label=label,
                fetchedAt=latest or _now_utc(),
                ttlSeconds=WARNINGS_TTL_SECONDS,
                errorReason=error_reason,
            ),
            entries=entries,
        )

    async def _get_detail_rows(self) -> list[dict[str, Any]]:
        if self._redis is not None:
            cached = await read_json(self._redis, _WARNINGS_KEY)
            now = _now_utc()
            if (
                cached
                and (now - cached["cachedAt"]).total_seconds() <= WARNINGS_TTL_SECONDS
            ):
                return list(cached["rows"])
        rows = await self._detail_fetcher()
        if self._redis is not None:
            now = _now_utc()
            await write_json(
                self._redis,
                _WARNINGS_KEY,
                {"rows": rows, "fetchedAt": now, "cachedAt": now},
                ex=WARNINGS_STALE_TOLERANCE_SECONDS,
            )
        return rows


async def db_universe_warnings_provider(
    markets: list[str] | None = None,
) -> UpbitMarketWarningsBlock:
    return await MarketWarningsService().get(markets)
