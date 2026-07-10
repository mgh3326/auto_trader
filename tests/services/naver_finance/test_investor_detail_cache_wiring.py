"""ROB-811 cache wiring in the opinion assembly."""

from __future__ import annotations

from typing import Any

import pytest
from bs4 import BeautifulSoup

from app.services.naver_finance import investor


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


def _list_soup() -> BeautifulSoup:
    html = """
    <table class="type_1"><tbody>
      <tr>
        <td>삼성전자</td>
        <td><a href="company_read.naver?nid=111">목표가 상향</a></td>
        <td>미래에셋</td><td>x</td><td>26.07.09</td>
      </tr>
      <tr>
        <td>삼성전자</td>
        <td><a href="company_read.naver?nid=222">유지</a></td>
        <td>KB증권</td><td>x</td><td>26.07.08</td>
      </tr>
    </tbody></table>
    """
    return BeautifulSoup(html, "lxml")


async def _build(detail_fetcher, detail_cache):
    return await investor._build_investment_opinions_from_company_list_soup(
        "005930",
        _list_soup(),
        limit=10,
        current_price=100000,
        detail_fetcher=detail_fetcher,
        detail_cache=detail_cache,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_hits_makes_zero_fetches() -> None:
    calls: list[str] = []

    async def fetcher(nid: str) -> dict[str, Any]:
        calls.append(nid)
        return {"target_price": 1, "rating": "x"}

    cache = FakeCache(
        {
            "111": {"target_price": 160000, "rating": "매수"},
            "222": {"target_price": None, "rating": None},
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
    async def fetcher(nid: str) -> dict[str, Any]:
        return {"target_price": 170000 if nid == "111" else None, "rating": "매수"}

    cache = FakeCache()
    await _build(fetcher, cache)
    assert cache.get_calls == [["111", "222"]]
    assert cache.put_calls == [
        {
            "111": {"target_price": 170000, "rating": "매수"},
            "222": {"target_price": None, "rating": "매수"},
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_failure_not_written() -> None:
    async def fetcher(nid: str) -> dict[str, Any] | None:
        return None if nid == "111" else {"target_price": 180000, "rating": "매수"}

    cache = FakeCache()
    result = await _build(fetcher, cache)
    assert list(cache.put_calls[0].keys()) == ["222"]  # 111 (None) not written
    tp = {o["title"]: o["target_price"] for o in result["opinions"]}
    assert tp["목표가 상향"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_none_cache_matches_legacy_behavior() -> None:
    async def fetcher(nid: str) -> dict[str, Any]:
        return {"target_price": 190000, "rating": "매수"}

    result = await _build(fetcher, None)  # detail_cache=None → legacy path
    assert result["count"] == 2
    assert all(o["target_price"] == 190000 for o in result["opinions"])