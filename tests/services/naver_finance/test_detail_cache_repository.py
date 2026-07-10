"""ROB-811 naver_research_detail_cache model + repository tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.db import AsyncSessionLocal
from app.models.naver_research_detail_cache import NaverResearchDetailCache
from app.services.naver_finance.detail_cache import (
    NaverResearchDetailCacheRepository,
    _coerce_target_price,
)


@pytest.mark.unit
def test_model_table_and_columns() -> None:
    assert NaverResearchDetailCache.__tablename__ == "naver_research_detail_cache"
    cols = set(NaverResearchDetailCache.__table__.columns.keys())
    assert cols == {"nid", "target_price", "rating", "fetched_at"}
    assert NaverResearchDetailCache.__table__.c.nid.primary_key is True


@pytest.mark.unit
def test_coerce_target_price_types() -> None:
    assert _coerce_target_price(None) is None
    assert _coerce_target_price(Decimal("150000")) == 150000
    assert isinstance(_coerce_target_price(Decimal("150000")), int)
    assert _coerce_target_price(Decimal("12.5")) == 12.5
    assert isinstance(_coerce_target_price(Decimal("12.5")), float)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_many_empty_returns_empty() -> None:
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        assert await repo.get_many([]) == {}
        assert await repo.get_many(["does-not-exist"]) == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_then_get_roundtrip_and_idempotent() -> None:
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        await repo.put_many(
            {
                "111": {"target_price": 150000, "rating": "매수"},
                "222": {"target_price": None, "rating": None},
            }
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        got = await repo.get_many(["111", "222", "333"])
        assert got == {
            "111": {"target_price": 150000, "rating": "매수"},
            "222": {"target_price": None, "rating": None},
        }
        assert isinstance(got["111"]["target_price"], int)

    # ON CONFLICT DO NOTHING: re-put with different values must not raise or overwrite
    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        await repo.put_many({"111": {"target_price": 999, "rating": "매도"}})
        await session.commit()

    async with AsyncSessionLocal() as session:
        repo = NaverResearchDetailCacheRepository(session)
        got = await repo.get_many(["111"])
        assert got["111"] == {"target_price": 150000, "rating": "매수"}