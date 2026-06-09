# ROB-398 — `get_symbol_news_mapping` MCP 도구 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 news-symbol mapping read-model을 신규 read-only MCP 도구 `get_symbol_news_mapping`으로 노출 — 심볼→매핑된 뉴스(symbol/mapping_source/confidence/is_primary + url + as_of)를 정직한 freshness와 함께 반환.

**Architecture:** 신규 DB `ArticleProvider`가 `get_news_articles_with_fallback`(exact→related→alias+dedup)로 기사를 가져와 `ArticleView`로 매핑하고(related_symbols는 detached lazy-load 회피 위해 article_id로 별도 조회), 기존 `get_symbol_news_mapping` query_service가 매핑/freshness를 수행, 신규 MCP 핸들러가 응답 dict로 포맷·등록. 계약(`ArticleView`/`MappedArticle`)에 url/summary를 additive 추가.

**Tech Stack:** Python 3.13, SQLAlchemy async (`AsyncSessionLocal`), FastMCP `@mcp.tool`, pytest(asyncio), ruff. migration 0, read-only.

---

## File Structure

- **Modify** `app/services/kr_news_symbol_mapping/contract.py` — `ArticleView.url`, `MappedArticle.url`/`.summary` 필드 추가(additive).
- **Modify** `app/services/kr_news_symbol_mapping/query_service.py` — `MappedArticle` 생성 시 url/summary 전달.
- **Create** `app/services/kr_news_symbol_mapping/db_provider.py` — `db_article_provider`(ArticleProvider 구현) + `_load_related_rows` + 순수 매퍼 `_candidate_rows_from_orm`/`_article_to_view`.
- **Create** `app/mcp_server/tooling/news_symbol_mapping.py` — `handle_get_symbol_news_mapping` + 순수 `_format_symbol_news_mapping`.
- **Modify** `app/mcp_server/tooling/news_handlers.py` — `@mcp.tool` 등록 + `NEWS_TOOL_NAMES` 추가.
- **Modify** `app/mcp_server/__init__.py` — `AVAILABLE_TOOL_NAMES`에 추가.
- **Tests**: extend `tests/test_kr_news_symbol_mapping_query.py`; create `tests/test_kr_news_symbol_mapping_db_provider.py`, `tests/test_news_symbol_mapping_handler.py`, `tests/test_mcp_news_symbol_mapping_tool.py`.

참조(읽기 전용): `app/services/llm_news_service.py::get_news_articles_with_fallback(*, symbol, market, hours=24, limit=20) -> NewsLookupResult(articles: list[NewsArticle], match_reasons)` (self-acquires sessions → 반환 article은 detached); `app/models/news.py::NewsArticle`(id, url, title, summary, keywords, stock_symbol, market, article_published_at, scraped_at) / `NewsArticleRelatedSymbol`(article_id, market, symbol, source, matched_term, score, rank).

---

## Task 1: 계약 확장 (ArticleView.url, MappedArticle.url/summary) + query_service passthrough

**Files:**
- Modify: `app/services/kr_news_symbol_mapping/contract.py`
- Modify: `app/services/kr_news_symbol_mapping/query_service.py`
- Test: `tests/test_kr_news_symbol_mapping_query.py`

- [ ] **Step 1: Write the failing test** (append to `tests/test_kr_news_symbol_mapping_query.py`)

```python
@pytest.mark.asyncio
async def test_url_and_summary_passthrough_to_mapped_article():
    async def provider(symbol, market, hours, limit):
        return [
            ArticleView(
                market="kr",
                stock_symbol="005930",
                related_rows=(),
                title="삼성전자 신규 투자",
                summary="리드 문장",
                keywords=(),
                as_of=NOW,
                url="https://n.news.naver.com/article/001/000",
            )
        ]

    result = await get_symbol_news_mapping(
        "005930", market="kr", now=NOW, article_provider=provider
    )
    assert len(result.articles) == 1
    art = result.articles[0]
    assert art.url == "https://n.news.naver.com/article/001/000"
    assert art.summary == "리드 문장"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_query.py::test_url_and_summary_passthrough_to_mapped_article -v`
Expected: FAIL — `TypeError: ArticleView.__init__() got an unexpected keyword argument 'url'`.

- [ ] **Step 3: Add the contract fields**

In `app/services/kr_news_symbol_mapping/contract.py`, add `url` to `ArticleView` (after `as_of`):

```python
@dataclass(frozen=True)
class ArticleView:
    """resolver/query_service 입력용 기사 뷰 (DB ORM 비의존, 테스트 친화)."""

    market: str
    stock_symbol: str | None
    related_rows: tuple[CandidateRow, ...]
    title: str | None
    summary: str | None
    keywords: tuple[str, ...]
    as_of: datetime
    url: str | None = None
```

And add `url`/`summary` to `MappedArticle` (after `mapped_symbols`):

```python
@dataclass(frozen=True)
class MappedArticle:
    as_of: datetime
    title: str | None
    mapped_symbols: tuple[MappedSymbol, ...]
    url: str | None = None
    summary: str | None = None
```

- [ ] **Step 4: Thread url/summary in query_service**

In `app/services/kr_news_symbol_mapping/query_service.py`, change the `MappedArticle(...)` construction (currently `MappedArticle(as_of=av.as_of, title=av.title, mapped_symbols=ordered)`):

```python
        mapped_articles.append(
            MappedArticle(
                as_of=av.as_of,
                title=av.title,
                mapped_symbols=ordered,
                url=av.url,
                summary=av.summary,
            )
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_query.py -v`
Expected: PASS (new test + all existing — defaults keep back-compat).

- [ ] **Step 6: Commit**

```bash
git add app/services/kr_news_symbol_mapping/contract.py app/services/kr_news_symbol_mapping/query_service.py tests/test_kr_news_symbol_mapping_query.py
git commit -m "feat(ROB-398): ArticleView.url + MappedArticle.url/summary passthrough"
```

---

## Task 2: 순수 매퍼 — ORM rows → CandidateRow, NewsArticle → ArticleView

**Files:**
- Create: `app/services/kr_news_symbol_mapping/db_provider.py`
- Test: `tests/test_kr_news_symbol_mapping_db_provider.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_news_symbol_mapping_db_provider.py
from datetime import UTC, datetime

import pytest

from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping import db_provider as dbp
from app.services.kr_news_symbol_mapping.contract import CandidateRow

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.unit
def test_candidate_rows_from_orm_maps_fields():
    rows = [
        NewsArticleRelatedSymbol(
            article_id=1, market="kr", symbol="035420", source="naver_code",
            matched_term=None, score=None, rank=1,
        ),
        NewsArticleRelatedSymbol(
            article_id=1, market="kr", symbol="000660", source="ner",
            matched_term="닉스", score=0.5, rank=2,
        ),
    ]
    out = dbp._candidate_rows_from_orm(rows)
    assert out == (
        CandidateRow(symbol="035420", source="naver_code", score=None, rank=1, matched_term=None),
        CandidateRow(symbol="000660", source="ner", score=0.5, rank=2, matched_term="닉스"),
    )


@pytest.mark.unit
def test_article_to_view_maps_fields_and_url():
    article = NewsArticle(
        id=7, market="kr", url="https://n.news.naver.com/a/1",
        title="네이버 GTC 언급", summary="리드", keywords=["AI"],
        stock_symbol="035420", article_published_at=NOW, scraped_at=NOW,
    )
    related = (CandidateRow(symbol="035420", source="naver_code"),)
    view = dbp._article_to_view(article, related)
    assert view.market == "kr"
    assert view.stock_symbol == "035420"
    assert view.related_rows == related
    assert view.title == "네이버 GTC 언급"
    assert view.summary == "리드"
    assert view.keywords == ("AI",)
    assert view.as_of == NOW
    assert view.url == "https://n.news.naver.com/a/1"


@pytest.mark.unit
def test_article_to_view_as_of_falls_back_to_scraped_at():
    article = NewsArticle(
        id=8, market="kr", url="https://x/2", title="t", summary=None,
        keywords=None, stock_symbol=None, article_published_at=None, scraped_at=NOW,
    )
    view = dbp._article_to_view(article, ())
    assert view.as_of == NOW  # published_at None -> scraped_at
    assert view.keywords == ()  # None -> ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_db_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.kr_news_symbol_mapping.db_provider`.

- [ ] **Step 3: Write the pure mappers**

```python
# app/services/kr_news_symbol_mapping/db_provider.py
"""DB-backed ArticleProvider for the kr_news_symbol_mapping read-model (ROB-398).

Adapts get_news_articles_with_fallback (exact->related->alias) + a separate
news_article_related_symbols lookup into ArticleView[] for get_symbol_news_mapping.
Read-only; self-acquires sessions; no writes.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping.contract import ArticleView, CandidateRow


def _candidate_rows_from_orm(
    rows: Sequence[NewsArticleRelatedSymbol],
) -> tuple[CandidateRow, ...]:
    return tuple(
        CandidateRow(
            symbol=r.symbol,
            source=r.source,
            score=r.score,
            rank=r.rank,
            matched_term=r.matched_term,
        )
        for r in rows
    )


def _article_to_view(
    article: NewsArticle, related_rows: tuple[CandidateRow, ...]
) -> ArticleView:
    return ArticleView(
        market=article.market,
        stock_symbol=article.stock_symbol,
        related_rows=related_rows,
        title=article.title,
        summary=article.summary,
        keywords=tuple(article.keywords or ()),
        as_of=article.article_published_at or article.scraped_at,
        url=article.url,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_db_provider.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/kr_news_symbol_mapping/db_provider.py tests/test_kr_news_symbol_mapping_db_provider.py
git commit -m "feat(ROB-398): db_provider pure mappers (ORM->CandidateRow, NewsArticle->ArticleView)"
```

---

## Task 3: `_load_related_rows` + `db_article_provider` orchestration

**Files:**
- Modify: `app/services/kr_news_symbol_mapping/db_provider.py`
- Test: `tests/test_kr_news_symbol_mapping_db_provider.py`

Detached-instance 회피: fallback이 반환한 article들의 `related_symbols`는 lazy-load 불가 → `_load_related_rows`가 article_id로 `NewsArticleRelatedSymbol`을 별도 세션에서 조회. `db_article_provider`는 fallback + `_load_related_rows`를 patch 가능한 모듈 함수로 호출하므로 DB 없이 단위 테스트.

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_db_article_provider_builds_views(monkeypatch):
    from app.services.llm_news_service import NewsLookupResult

    a1 = NewsArticle(
        id=1, market="kr", url="https://x/1", title="삼성", summary=None,
        keywords=["반도체"], stock_symbol="005930",
        article_published_at=NOW, scraped_at=NOW,
    )
    a2 = NewsArticle(
        id=2, market="kr", url="https://x/2", title="네이버", summary="리드",
        keywords=None, stock_symbol=None, article_published_at=None, scraped_at=NOW,
    )

    async def fake_fallback(*, symbol, market, hours, limit):
        return NewsLookupResult(articles=[a1, a2], match_reasons={})

    async def fake_load_related(article_ids):
        assert set(article_ids) == {1, 2}
        return {2: (CandidateRow(symbol="035420", source="naver_code"),)}

    monkeypatch.setattr(dbp, "get_news_articles_with_fallback", fake_fallback)
    monkeypatch.setattr(dbp, "_load_related_rows", fake_load_related)

    views = await dbp.db_article_provider("005930", "kr", 24, 20)
    assert [v.url for v in views] == ["https://x/1", "https://x/2"]
    assert views[0].related_rows == ()          # a1 had no related rows
    assert views[1].related_rows == (CandidateRow(symbol="035420", source="naver_code"),)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_db_article_provider_empty_when_no_articles(monkeypatch):
    from app.services.llm_news_service import NewsLookupResult

    async def fake_fallback(*, symbol, market, hours, limit):
        return NewsLookupResult(articles=[], match_reasons={})

    monkeypatch.setattr(dbp, "get_news_articles_with_fallback", fake_fallback)
    views = await dbp.db_article_provider("999999", "kr", 24, 20)
    assert views == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_db_provider.py -k db_article_provider -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'db_article_provider'`.

- [ ] **Step 3: Implement orchestration** (append to `db_provider.py`)

```python
from app.services.llm_news_service import get_news_articles_with_fallback


async def _load_related_rows(
    article_ids: Sequence[int],
) -> dict[int, tuple[CandidateRow, ...]]:
    """Load news_article_related_symbols for the given article ids (own session,
    avoids DetachedInstanceError from lazy article.related_symbols)."""
    if not article_ids:
        return {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id.in_(list(article_ids))
            )
        )
        rows = result.scalars().all()
    grouped: dict[int, list[NewsArticleRelatedSymbol]] = {}
    for row in rows:
        grouped.setdefault(row.article_id, []).append(row)
    return {aid: _candidate_rows_from_orm(rs) for aid, rs in grouped.items()}


async def db_article_provider(
    symbol: str, market: str, hours: int, limit: int
) -> list[ArticleView]:
    """ArticleProvider: symbol-targeted articles (exact->related->alias) mapped to
    ArticleView with per-article related_symbols. Positional signature matches the
    ArticleProvider contract; fail-open is the caller's concern (returns [] if empty)."""
    lookup = await get_news_articles_with_fallback(
        symbol=symbol, market=market, hours=hours, limit=limit
    )
    if not lookup.articles:
        return []
    related = await _load_related_rows([a.id for a in lookup.articles])
    return [_article_to_view(a, related.get(a.id, ())) for a in lookup.articles]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_db_provider.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/kr_news_symbol_mapping/db_provider.py tests/test_kr_news_symbol_mapping_db_provider.py
git commit -m "feat(ROB-398): db_article_provider + _load_related_rows (detached-safe)"
```

---

## Task 4: MCP 핸들러 — handle_get_symbol_news_mapping + _format_symbol_news_mapping

**Files:**
- Create: `app/mcp_server/tooling/news_symbol_mapping.py`
- Test: `tests/test_news_symbol_mapping_handler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_news_symbol_mapping_handler.py
from datetime import UTC, datetime

import pytest

from app.mcp_server.tooling import news_symbol_mapping as nsm
from app.services.kr_news_symbol_mapping.contract import ArticleView

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_returns_mapped_symbols_and_url():
    async def provider(symbol, market, hours, limit):
        return [
            ArticleView(
                market="kr", stock_symbol="035420", related_rows=(),
                title="네이버 GTC", summary="리드", keywords=(), as_of=NOW,
                url="https://n.news.naver.com/a/1",
            )
        ]

    resp = await nsm.handle_get_symbol_news_mapping(
        symbol="035420", market="kr", now=NOW, article_provider=provider
    )
    assert resp["symbol"] == "035420"
    assert resp["market"] == "kr"
    assert resp["data_state"] == "fresh"
    assert len(resp["articles"]) == 1
    art = resp["articles"][0]
    assert art["url"] == "https://n.news.naver.com/a/1"
    assert art["summary"] == "리드"
    assert art["mapped_symbols"][0]["symbol"] == "035420"
    assert art["mapped_symbols"][0]["mapping_source"] == "naver_code"
    assert art["mapped_symbols"][0]["is_primary"] is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_unavailable_is_honest_not_error():
    async def empty_provider(symbol, market, hours, limit):
        return []

    resp = await nsm.handle_get_symbol_news_mapping(
        symbol="999999", market="kr", now=NOW, article_provider=empty_provider
    )
    assert resp["data_state"] == "unavailable"
    assert resp["articles"] == []
    assert any("매핑된 뉴스가 없" in w for w in resp["warnings"])
    assert "error" not in resp
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_news_symbol_mapping_handler.py -v`
Expected: FAIL — `ModuleNotFoundError: app.mcp_server.tooling.news_symbol_mapping`.

- [ ] **Step 3: Write the handler**

```python
# app/mcp_server/tooling/news_symbol_mapping.py
"""MCP handler for get_symbol_news_mapping (ROB-398 surface slice 1).

Exposes the news-symbol mapping read-model: symbol -> mapped news (symbol /
mapping_source / confidence / is_primary) + url + as_of, with honest data_state.
Read-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.kr_news_symbol_mapping.contract import SymbolNewsMapping
from app.services.kr_news_symbol_mapping.db_provider import db_article_provider
from app.services.kr_news_symbol_mapping.query_service import (
    ArticleProvider,
    get_symbol_news_mapping,
)


def _format_symbol_news_mapping(mapping: SymbolNewsMapping) -> dict[str, Any]:
    data_state = mapping.freshness.overall
    articles = [
        {
            "title": a.title,
            "url": a.url,
            "summary": a.summary,
            "as_of": a.as_of.isoformat() if a.as_of else None,
            "mapped_symbols": [
                {
                    "symbol": s.symbol,
                    "market": s.market,
                    "mapping_source": s.mapping_source,
                    "confidence": s.confidence,
                    "is_primary": s.is_primary,
                    "matched_term": s.matched_term,
                }
                for s in a.mapped_symbols
            ],
        }
        for a in mapping.articles
    ]
    warnings: list[str] = []
    if data_state == "unavailable":
        warnings.append("해당 종목에 매핑된 뉴스가 없습니다 (최근 윈도우 내).")
    elif data_state == "stale":
        warnings.append("매핑된 뉴스가 오래되었습니다 — 신선도에 주의하세요.")
    return {
        "symbol": mapping.symbol,
        "market": mapping.market,
        "data_state": data_state,
        "latest_as_of": (
            mapping.freshness.latest_as_of.isoformat()
            if mapping.freshness.latest_as_of
            else None
        ),
        "articles": articles,
        "warnings": warnings,
    }


async def handle_get_symbol_news_mapping(
    *,
    symbol: str,
    market: str = "kr",
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
    article_provider: ArticleProvider | None = None,
) -> dict[str, Any]:
    mapping = await get_symbol_news_mapping(
        symbol,
        market=market,
        hours=hours,
        limit=limit,
        now=now,
        article_provider=article_provider or db_article_provider,
    )
    return _format_symbol_news_mapping(mapping)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_news_symbol_mapping_handler.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/news_symbol_mapping.py tests/test_news_symbol_mapping_handler.py
git commit -m "feat(ROB-398): get_symbol_news_mapping MCP handler + honest formatter"
```

---

## Task 5: 도구 등록 (news_handlers + NEWS_TOOL_NAMES + AVAILABLE_TOOL_NAMES)

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py`
- Modify: `app/mcp_server/__init__.py`
- Test: `tests/test_mcp_news_symbol_mapping_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mcp_news_symbol_mapping_tool.py
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server import AVAILABLE_TOOL_NAMES
from app.mcp_server.tooling.news_handlers import NEWS_TOOL_NAMES
from tests._mcp_tooling_support import build_tools

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.unit
def test_tool_registered():
    assert "get_symbol_news_mapping" in NEWS_TOOL_NAMES
    assert "get_symbol_news_mapping" in AVAILABLE_TOOL_NAMES
    tools = build_tools()
    assert "get_symbol_news_mapping" in tools


@pytest.mark.asyncio
@pytest.mark.unit
async def test_tool_invokes_handler():
    tools = build_tools()
    fake_resp = {"symbol": "035420", "market": "kr", "data_state": "fresh",
                 "latest_as_of": None, "articles": [], "warnings": []}
    with patch(
        "app.mcp_server.tooling.news_symbol_mapping.handle_get_symbol_news_mapping",
        new=AsyncMock(return_value=fake_resp),
    ) as mock_handle:
        result = await tools["get_symbol_news_mapping"](symbol="035420", market="kr")
    assert result["symbol"] == "035420"
    mock_handle.assert_awaited_once()
```

> Before implementing, confirm how `build_tools()` (in `tests/_mcp_tooling_support.py`) discovers registered tools — it must register the news tools (`register_news_tools` / `_register_news_tools_impl`). If `build_tools` doesn't already include the news registration, add the new tool's registration path the same way the existing `get_market_news` is reachable there (mirror it). Adjust the patch target if the registered inner function imports the handler lazily vs at module top.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_news_symbol_mapping_tool.py -v`
Expected: FAIL — `assert "get_symbol_news_mapping" in NEWS_TOOL_NAMES` fails (not registered).

- [ ] **Step 3: Register the tool**

In `app/mcp_server/tooling/news_handlers.py`, update line 19:

```python
NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues", "get_symbol_news_mapping"]
```

Inside `_register_news_tools_impl(mcp)` (after the `get_market_issues` tool block, before the function ends), add:

```python
    @mcp.tool(
        name="get_symbol_news_mapping",
        description=(
            "Read-only: news mapped to a stock symbol from the news-symbol mapping "
            "read-model (ROB-398). Returns per-article mapped_symbols "
            "(symbol/mapping_source[naver_code|candidate|ner]/confidence/is_primary) "
            "plus title/url/as_of and an honest data_state (fresh|stale|unavailable). "
            "Use for symbol-level news evidence; empty mapping returns unavailable, not error."
        ),
    )
    async def get_symbol_news_mapping(
        symbol: str,
        market: str = "kr",
        hours: int = 24,
        limit: int = 20,
    ) -> dict[str, Any]:
        from app.mcp_server.tooling.news_symbol_mapping import (
            handle_get_symbol_news_mapping,
        )

        return await handle_get_symbol_news_mapping(
            symbol=symbol, market=market, hours=hours, limit=limit
        )
```

In `app/mcp_server/__init__.py`, add `"get_symbol_news_mapping"` to the `AVAILABLE_TOOL_NAMES` list, immediately after the `"get_news"` entry (line 24):

```python
    "get_news",
    "get_symbol_news_mapping",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_news_symbol_mapping_tool.py -v`
Expected: PASS (2 tests). If `test_tool_invokes_handler` patch target mismatches (lazy import), patch `app.mcp_server.tooling.news_handlers`-level reference per the Step 1 note.

- [ ] **Step 5: Run the full slice + lint**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_query.py tests/test_kr_news_symbol_mapping_db_provider.py tests/test_news_symbol_mapping_handler.py tests/test_mcp_news_symbol_mapping_tool.py -v`
Expected: all PASS.
Run: `uv run ruff check app/services/kr_news_symbol_mapping/ app/mcp_server/tooling/news_symbol_mapping.py app/mcp_server/tooling/news_handlers.py app/mcp_server/__init__.py tests/test_kr_news_symbol_mapping_db_provider.py tests/test_news_symbol_mapping_handler.py tests/test_mcp_news_symbol_mapping_tool.py`
Expected: `All checks passed!` (run `ruff format` on the same paths if needed).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py app/mcp_server/__init__.py tests/test_mcp_news_symbol_mapping_tool.py
git commit -m "feat(ROB-398): register get_symbol_news_mapping MCP tool"
```

---

## Self-Review

**1. Spec coverage:**
- §3 D1 신규 도구 → Task 5 (등록). ✓
- §3 D2 백킹=get_news_articles_with_fallback → Task 3 (`db_article_provider`). ✓
- §3 D3 출력 매핑+url, summary best-effort → Task 1 (계약) + Task 4 (formatter). ✓
- §3 D4 / §7 정직 리포팅 (data_state, unavailable=빈+warning) → Task 4 (`_format_symbol_news_mapping`). ✓
- §5 related_symbols detached-safe 로딩 → Task 3 (`_load_related_rows` 별도 세션). ✓
- §6 응답 shape → Task 4. ✓
- §8 테스트(provider/passthrough/handler/integration/empty) → Tasks 1–5 tests. ✓
- §9 비범위 (get_market_news/get_news/search_news/collection) → 미접근. ✓ migration 0. ✓

**2. Placeholder scan:** No TBD/TODO. Two verification notes (build_tools discovery in Task 5; remove unused `Any` import in Task 3) are explicit instructions, not placeholders. ✓

**3. Type consistency:** `ArticleView.url`/`MappedArticle.url`/`.summary` (Task 1) consumed by `_article_to_view` (Task 2) and `_format_symbol_news_mapping` (Task 4). `db_article_provider(symbol, market, hours, limit)` positional signature (Task 3) matches `ArticleProvider = Callable[[str,str,int,int], ...]` from query_service. `handle_get_symbol_news_mapping(article_provider=...)` defaults to `db_article_provider` (Task 4) and is invoked by the registered tool (Task 5). `_candidate_rows_from_orm`/`_load_related_rows`/`_article_to_view` names consistent across Tasks 2–3. ✓

**Open verification flags for the implementer (resolve while implementing, do not guess):**
- `build_tools()` news-tool discovery + the correct patch target for `test_tool_invokes_handler` (lazy import) — Task 5 Step 1 note.
- Whether constructing `NewsArticle(...)`/`NewsArticleRelatedSymbol(...)` in-memory (no flush) is accepted by the test DB config — if a test plugin auto-flushes, build plain `SimpleNamespace` stand-ins exposing the same attributes instead (the pure mappers only read attributes).

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
