"""Naver research detail-page cache repository/store (ROB-811).

Sole writer for `naver_research_detail_cache`. The store owns short-lived
sessions and swallows DB errors so a cache fault degrades to uncached scraping.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.naver_research_detail_cache import NaverResearchDetailCache
from app.services.naver_finance.detail_cache_port import DetailCachePort

logger = logging.getLogger(__name__)


def _coerce_target_price(v: Decimal | int | float | None) -> int | float | None:
    """Reconstruct the int|float that parse_korean_number produced.

    The Numeric column round-trips to Decimal; integral values become int and
    fractional values become float so downstream arithmetic never mixes Decimal
    with float.
    """
    if v is None:
        return None
    d = Decimal(v)
    if d == d.to_integral_value():
        return int(d)
    return float(d)


class NaverResearchDetailCacheRepository:
    """Sole writer for naver_research_detail_cache."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]:
        if not nids:
            return {}
        rows = (
            await self.db.execute(
                select(NaverResearchDetailCache).where(
                    NaverResearchDetailCache.nid.in_(nids)
                )
            )
        ).scalars().all()
        return {
            row.nid: {
                "target_price": _coerce_target_price(row.target_price),
                "rating": row.rating,
            }
            for row in rows
        }

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None:
        if not entries:
            return
        values = [
            {
                "nid": nid,
                "target_price": detail.get("target_price"),
                "rating": detail.get("rating"),
            }
            for nid, detail in entries.items()
        ]
        stmt = (
            pg_insert(NaverResearchDetailCache)
            .values(values)
            .on_conflict_do_nothing(index_elements=["nid"])
        )
        await self.db.execute(stmt)


class NaverResearchDetailCacheStore:
    """DetailCachePort backed by naver_research_detail_cache.

    Owns a short-lived session per call. Any DB error is swallowed so analysis
    degrades to uncached scraping rather than failing.
    """

    async def get_many(self, nids: list[str]) -> dict[str, Any]:
        if not nids:
            return {}
        try:
            async with AsyncSessionLocal() as session:
                repo = NaverResearchDetailCacheRepository(session)
                return await repo.get_many(nids)
        except Exception:  # pragma: no cover - defensive
            logger.warning("naver detail cache get_many failed", exc_info=True)
            return {}

    async def put_many(self, entries: dict[str, Any]) -> None:
        if not entries:
            return
        try:
            async with AsyncSessionLocal() as session:
                repo = NaverResearchDetailCacheRepository(session)
                await repo.put_many(entries)
                await session.commit()
        except Exception:  # pragma: no cover - defensive
            logger.warning("naver detail cache put_many failed", exc_info=True)


def get_detail_cache() -> DetailCachePort | None:
    """Return a store, or None when disabled via env (default enabled)."""
    if os.getenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", "true").strip().lower() != "true":
        return None
    return NaverResearchDetailCacheStore()