"""ROB-811 cache wiring in the opinion assembly (re-keyed for #930)."""

from __future__ import annotations

from typing import Any

import pytest

from app.mcp_server.tooling import fundamentals_sources_naver
from app.services.naver_finance import investor
from app.services.naver_finance.investor import NaverResearchContractError


class FakeCache:
    def __init__(self, seeded: dict[str, dict[str, Any]] | None = None) -> None:
        self.store: dict[str, dict[str, Any]] = dict(seeded or {})
        self.get_calls: list[list[str]] = []
        self.put_calls: list[dict[str, dict[str, Any]]] = []

    async def get_many(self, nids: list[str]) -> dict[str, dict[str, Any]]:
        self.get_calls.append(list(nids))
        return {n: self.store[n] for n in nids if n in self.store}

    async def put_many(self, entries: dict[str, dict[str, Any]]) -> None:
        self.put_calls.append(dict(entries))
        self.store.update(entries)


def _list_items() -> list[dict[str, Any]]:
    """Normalized research-list rows (#930): research_id keys the cache."""
    return [
        {
            "research_id": 111,
            "stock_name": "삼성전자",
            "title": "목표가 상향",
            "firm": "미래에셋",
            "date": "2026-07-09",
            "url": "https://m.stock.naver.com/research/company/111",
        },
        {
            "research_id": 222,
            "stock_name": "삼성전자",
            "title": "유지",
            "firm": "KB증권",
            "date": "2026-07-08",
            "url": "https://m.stock.naver.com/research/company/222",
        },
    ]


async def _build(detail_fetcher, detail_cache):
    return await investor._build_investment_opinions_from_research_items(
        "005930",
        _list_items(),
        limit=10,
        current_price=100000,
        detail_fetcher=detail_fetcher,
        detail_cache=detail_cache,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_hits_makes_zero_fetches() -> None:
    calls: list[int] = []

    async def fetcher(research_id: int) -> dict[str, Any]:
        calls.append(research_id)
        return {"target_price": 1, "rating": "x"}

    cache = FakeCache(
        {
            # #930: research-api rows are namespaced so they can never collide
            # with a legacy company_read nid key.
            "api:111": {"target_price": 160000, "rating": "매수"},
            "api:222": {"target_price": None, "rating": None},
        }
    )
    result = await _build(fetcher, cache)
    assert calls == []  # no HTTP detail calls
    assert cache.put_calls == []  # nothing new to write
    tp = {o["title"]: o["target_price"] for o in result["opinions"]}
    assert tp == {"목표가 상향": 160000, "유지": None}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_miss_fetches_and_writes() -> None:
    async def fetcher(research_id: int) -> dict[str, Any]:
        return {
            "target_price": 170000 if research_id == 111 else None,
            "rating": "매수",
        }

    cache = FakeCache()
    await _build(fetcher, cache)
    assert cache.get_calls == [["api:111", "api:222"]]
    assert cache.put_calls == [
        {
            "api:111": {"target_price": 170000, "rating": "매수"},
            "api:222": {"target_price": None, "rating": "매수"},
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_failure_not_written() -> None:
    async def fetcher(research_id: int) -> dict[str, Any]:
        if research_id == 111:
            raise RuntimeError("detail boom")
        return {"target_price": 180000, "rating": "매수"}

    cache = FakeCache()
    result = await _build(fetcher, cache)
    assert list(cache.put_calls[0].keys()) == ["api:222"]  # 111 failure not written
    tp = {o["title"]: o["target_price"] for o in result["opinions"]}
    assert tp["목표가 상향"] is None
    assert result["opinions"][0]["rating_bucket"] == "unrated"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_none_cache_matches_legacy_behavior() -> None:
    async def fetcher(research_id: int) -> dict[str, Any]:
        return {"target_price": 190000, "rating": "매수"}

    result = await _build(fetcher, None)  # detail_cache=None → legacy path
    assert result["count"] == 2
    assert all(o["target_price"] == 190000 for o in result["opinions"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrapper_passes_cache_to_fetch_investment_opinions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_fetch(symbol, limit=10, *, window_months=12, detail_cache=None):
        seen["detail_cache"] = detail_cache
        return {"symbol": symbol, "count": 0, "opinions": [], "consensus": None}

    monkeypatch.setattr(
        fundamentals_sources_naver.naver_finance,
        "fetch_investment_opinions",
        fake_fetch,
    )
    monkeypatch.delenv("NAVER_RESEARCH_DETAIL_CACHE_ENABLED", raising=False)
    await fundamentals_sources_naver._fetch_investment_opinions_naver("005930", 10)
    assert seen["detail_cache"] is not None  # injected store


# ---------------------------------------------------------------------------
# ROB-814 — anomalous detail payloads must NOT be cache-worthy
# ---------------------------------------------------------------------------


def test_detail_payload_contract_break_raises_not_cached() -> None:
    """ROB-814, #930 re-cut: a payload WITHOUT researchContent (contract break —
    anti-bot page shape served as JSON, payload drift) is an anomaly, not a
    report with a legitimately-absent target. The parser must raise so the
    assembly treats it like a fetch failure — surfaced as unrated + warning and
    NEVER written to the insert-once cache (which would freeze the anomaly
    permanently, surviving even a parser fix)."""
    with pytest.raises(NaverResearchContractError, match="researchContent"):
        investor._parse_research_detail_payload("005930", 111, {"junk": []})


def test_detail_payload_without_fields_stays_cacheworthy() -> None:
    """ROB-814 regression lock (#930 shape): a VALID researchContent whose
    opinion/goalPrice fields are simply absent is a real report without a
    target — the all-None dict stays cache-worthy (the ROB-811
    'success-with-no-target' rule preserved)."""
    payload = {
        "researchContent": {
            "itemCode": "005930",
            "researchId": 111,
            "opinion": None,
            "goalPrice": None,
        }
    }
    assert investor._parse_research_detail_payload("005930", 111, payload) == {
        "target_price": None,
        "rating": None,
    }
