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


def _entries_from_rows(
    rows: list[UpbitSymbolUniverse],
) -> dict[str, UpbitMarketWarningEntry]:
    return {
        row.market.upper(): UpbitMarketWarningEntry(
            market=row.market.upper(), warning=(row.market_warning or "NONE")
        )
        for row in rows
    }


def _merge_event_details(
    entries: dict[str, UpbitMarketWarningEntry], details: list[dict[str, Any]]
) -> dict[str, UpbitMarketWarningEntry]:
    by_market = {str(item.get("market") or "").upper(): item for item in details}
    return {
        market: entry.model_copy(
            update={"event": by_market.get(market, {}).get("market_event")}
        )
        for market, entry in entries.items()
    }


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
        rows = await self._load_rows(requested=requested, db=db)
        latest = max((getattr(row, "updated_at", None) for row in rows), default=None)
        entries = _entries_from_rows(rows)
        state = "fresh" if entries else "missing"
        error_reason = None

        if include_event_detail and entries:
            try:
                details = await self._get_detail_rows()
                entries = _merge_event_details(entries, details)
            except Exception as exc:  # noqa: BLE001
                state = "stale" if entries else "unavailable"
                error_reason = classify_error(exc)

        return UpbitMarketWarningsBlock(
            meta=UpbitBlockMeta(
                source="upbit_market_warnings",
                state=state,
                label="Upbit market warnings (universe)",
                fetchedAt=latest or _now_utc(),
                ttlSeconds=WARNINGS_TTL_SECONDS,
                errorReason=error_reason,
            ),
            entries=entries,
        )

    async def _load_rows(
        self, *, requested: set[str] | None, db: AsyncSession | None
    ) -> list[UpbitSymbolUniverse]:
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

        if db is not None:
            return await load(db)
        async with self._db_session_factory() as session:
            return await load(session)

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


_default_service = MarketWarningsService()
get_market_warnings = _default_service.get
db_universe_warnings_provider = _default_service.get

__all__ = [
    "MarketWarningsProvider",
    "MarketWarningsService",
    "db_universe_warnings_provider",
    "fetch_market_event_details",
    "get_market_warnings",
]
