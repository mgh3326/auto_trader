"""ROB-811 detail-cache store + factory tests."""

from __future__ import annotations

import pytest

from app.services.naver_finance import detail_cache
from app.services.naver_finance.detail_cache import (
    NaverResearchDetailCacheStore,
    get_detail_cache,
)


@pytest.mark.unit
def test_factory_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", "false")
    assert get_detail_cache() is None


@pytest.mark.unit
def test_factory_default_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    assert isinstance(get_detail_cache(), NaverResearchDetailCacheStore)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_swallows_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def __call__(self) -> _Boom:
            return self

        async def __aenter__(self) -> None:
            raise RuntimeError("db down")

        async def __aexit__(self, *a: object) -> None:
            return None

    monkeypatch.setattr(detail_cache, "AsyncSessionLocal", _Boom())
    store = NaverResearchDetailCacheStore()
    assert await store.get_many(["1"]) == {}
    # must not raise
    await store.put_many({"1": {"target_price": 1, "rating": "x"}})


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    store = get_detail_cache()
    assert store is not None
    await store.put_many({"901": {"target_price": 42000, "rating": "매수"}})
    got = await store.get_many(["901"])
    assert got == {"901": {"target_price": 42000, "rating": "매수"}}
