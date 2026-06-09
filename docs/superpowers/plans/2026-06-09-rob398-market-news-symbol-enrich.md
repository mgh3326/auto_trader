# ROB-398 — `get_market_news` symbol enrich Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_market_news`의 각 뉴스 아이템에 `mapped_symbols`(symbol/mapping_source/confidence/is_primary/matched_term)를 추가 — persisted `news_article_related_symbols` + live NER 둘 다 반영.

**Architecture:** 신규 공유 배치 로더가 article_id 묶음으로 `news_article_related_symbols`를 조회(detached-safe), `compute_mapped_symbols`가 기존 `match_symbols_for_article`+`resolve_article_symbols`(순수, main)로 per-article 매핑을 만들고, `_article_to_dict`/`_briefing_sections_to_dict`가 `mapped_by_id`로 enrich. PR1과 독립.

**Tech Stack:** Python 3.13, SQLAlchemy async (`AsyncSessionLocal`), pytest(asyncio), ruff. migration 0, read-only.

---

## File Structure

- **Create** `app/services/kr_news_symbol_mapping/related_lookup.py` — `load_related_rows_by_article_ids` + 순수 `_group_rows`.
- **Modify** `app/mcp_server/tooling/news_handlers.py` — `_mapped_symbol_to_dict` + `compute_mapped_symbols` 헬퍼; `_article_to_dict`/`_briefing_sections_to_dict`에 `mapped_by_id` 파라미터; `_get_market_news_impl`에 배치 로드 + 스레딩.
- **Tests**: create `tests/test_kr_news_symbol_mapping_related_lookup.py`; create `tests/test_market_news_symbol_enrich.py`.

참조(읽기 전용): `resolve_article_symbols(*, market, stock_symbol, related_rows: Sequence[CandidateRow], ner_matches: Sequence[SymbolMatch]) -> list[MappedSymbol]` (resolver.py); `match_symbols_for_article(title=, summary=, keywords=, market=) -> Sequence[SymbolMatch]` (news_entity_matcher.py); `CandidateRow`/`MappedSymbol` (contract.py); `NewsArticleRelatedSymbol`(article_id, market, symbol, source, matched_term, score, rank) / `NewsArticle`(id, title, summary, keywords, stock_symbol, market, url, source, feed_source, article_published_at, stock_name) (models/news.py). 알리아스 사전에 `삼성전자→005930` 존재(KR_ALIASES, news_entity_alias_data.py).

---

## Task 1: 공유 배치 로더 — `load_related_rows_by_article_ids`

**Files:**
- Create: `app/services/kr_news_symbol_mapping/related_lookup.py`
- Test: `tests/test_kr_news_symbol_mapping_related_lookup.py`

순수 그룹핑 `_group_rows`를 단위 테스트하고, 세션 부분(`load_related_rows_by_article_ids`)은 empty-case + 그룹핑 위임으로 얇게 유지(실 쿼리는 단순 `WHERE article_id IN`; e2e[Task 4]는 로더를 patch).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_kr_news_symbol_mapping_related_lookup.py
import pytest

from app.models.news import NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping import related_lookup as rl
from app.services.kr_news_symbol_mapping.contract import CandidateRow


@pytest.mark.unit
def test_group_rows_groups_by_article_id_to_candidate_rows():
    rows = [
        NewsArticleRelatedSymbol(article_id=1, market="kr", symbol="035420",
                                 source="naver_code", matched_term=None, score=None, rank=1),
        NewsArticleRelatedSymbol(article_id=1, market="kr", symbol="000660",
                                 source="ner", matched_term="닉스", score=0.5, rank=2),
        NewsArticleRelatedSymbol(article_id=2, market="kr", symbol="005930",
                                 source="candidate", matched_term=None, score=0.8, rank=1),
    ]
    out = rl._group_rows(rows)
    assert out[1] == (
        CandidateRow(symbol="035420", source="naver_code", score=None, rank=1, matched_term=None),
        CandidateRow(symbol="000660", source="ner", score=0.5, rank=2, matched_term="닉스"),
    )
    assert out[2] == (
        CandidateRow(symbol="005930", source="candidate", score=0.8, rank=1, matched_term=None),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_returns_empty_for_no_ids():
    assert await rl.load_related_rows_by_article_ids([]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_related_lookup.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.kr_news_symbol_mapping.related_lookup`.

- [ ] **Step 3: Write the implementation**

```python
# app/services/kr_news_symbol_mapping/related_lookup.py
"""Batch loader: news_article_related_symbols grouped by article_id (ROB-398).

Shared, read-only, self-acquires session. Avoids DetachedInstanceError from lazy
article.related_symbols when callers hold detached NewsArticle objects."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.news import NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping.contract import CandidateRow


def _group_rows(
    rows: Sequence[NewsArticleRelatedSymbol],
) -> dict[int, tuple[CandidateRow, ...]]:
    grouped: dict[int, list[CandidateRow]] = {}
    for row in rows:
        grouped.setdefault(row.article_id, []).append(
            CandidateRow(
                symbol=row.symbol,
                source=row.source,
                score=row.score,
                rank=row.rank,
                matched_term=row.matched_term,
            )
        )
    return {aid: tuple(rs) for aid, rs in grouped.items()}


async def load_related_rows_by_article_ids(
    article_ids: Sequence[int],
) -> dict[int, tuple[CandidateRow, ...]]:
    if not article_ids:
        return {}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsArticleRelatedSymbol).where(
                NewsArticleRelatedSymbol.article_id.in_(list(article_ids))
            )
        )
        rows = result.scalars().all()
    return _group_rows(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_related_lookup.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/kr_news_symbol_mapping/related_lookup.py tests/test_kr_news_symbol_mapping_related_lookup.py
git commit -m "feat(ROB-398): shared load_related_rows_by_article_ids batch loader"
```

---

## Task 2: `compute_mapped_symbols` + `_mapped_symbol_to_dict`

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py`
- Test: `tests/test_market_news_symbol_enrich.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_market_news_symbol_enrich.py
import pytest

from app.models.news import NewsArticle
from app.mcp_server.tooling import news_handlers as nh
from app.services.kr_news_symbol_mapping.contract import CandidateRow


@pytest.mark.unit
def test_compute_mapped_symbols_naver_code_wins_over_ner():
    # stock_symbol confirmed AND title NER-matches the same symbol -> naver_code wins
    article = NewsArticle(
        id=1, market="kr", title="삼성전자 신규 투자", summary=None,
        keywords=None, stock_symbol="005930",
    )
    out = nh.compute_mapped_symbols(article, ())
    hit = next(m for m in out if m["symbol"] == "005930")
    assert hit["mapping_source"] == "naver_code"
    assert hit["is_primary"] is True
    assert hit["confidence"] == 1.0


@pytest.mark.unit
def test_compute_mapped_symbols_mainnews_ner_only():
    # mainnews: no stock_symbol, no persisted rows, but title mentions 삼성전자 -> NER maps
    article = NewsArticle(
        id=2, market="kr", title="삼성전자 사옥 방문", summary=None,
        keywords=None, stock_symbol=None,
    )
    out = nh.compute_mapped_symbols(article, ())
    hit = next(m for m in out if m["symbol"] == "005930")
    assert hit["mapping_source"] == "ner"


@pytest.mark.unit
def test_compute_mapped_symbols_persisted_candidate_row():
    article = NewsArticle(
        id=3, market="kr", title="오늘 증시 코멘트", summary=None,
        keywords=None, stock_symbol=None,
    )
    out = nh.compute_mapped_symbols(
        article, (CandidateRow(symbol="000660", source="candidate", score=0.8),)
    )
    hit = next(m for m in out if m["symbol"] == "000660")
    assert hit["mapping_source"] == "candidate"
    assert hit["confidence"] == 0.8


@pytest.mark.unit
def test_compute_mapped_symbols_empty_when_no_match():
    article = NewsArticle(
        id=4, market="kr", title="오늘 날씨는 맑음", summary=None,
        keywords=None, stock_symbol=None,
    )
    assert nh.compute_mapped_symbols(article, ()) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -k compute_mapped_symbols -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'compute_mapped_symbols'`.

- [ ] **Step 3: Add the helpers**

In `app/mcp_server/tooling/news_handlers.py`, add imports near the top (after the existing `from app.models.news import NewsArticle`):

```python
from app.services.kr_news_symbol_mapping.contract import CandidateRow, MappedSymbol
from app.services.kr_news_symbol_mapping.related_lookup import (
    load_related_rows_by_article_ids,
)
from app.services.kr_news_symbol_mapping.resolver import resolve_article_symbols
from app.services.news_entity_matcher import match_symbols_for_article
```

Then add the helpers (e.g., right after `NEWS_TOOL_NAMES = [...]`):

```python
def _mapped_symbol_to_dict(symbol: MappedSymbol) -> dict[str, Any]:
    return {
        "symbol": symbol.symbol,
        "market": symbol.market,
        "mapping_source": symbol.mapping_source,
        "confidence": symbol.confidence,
        "is_primary": symbol.is_primary,
        "matched_term": symbol.matched_term,
    }


def compute_mapped_symbols(
    article: NewsArticle, related_rows: tuple[CandidateRow, ...]
) -> list[dict[str, Any]]:
    """Per-article symbol mapping for market news: persisted related rows + live NER,
    resolved by the shared resolver (naver_code > candidate > ner). [] if no match."""
    ner_matches = match_symbols_for_article(
        title=article.title,
        summary=article.summary,
        keywords=article.keywords or [],
        market=article.market,
    )
    mapped = resolve_article_symbols(
        market=article.market,
        stock_symbol=article.stock_symbol,
        related_rows=related_rows,
        ner_matches=ner_matches,
    )
    return [_mapped_symbol_to_dict(m) for m in mapped]
```

> Verify the exact `match_symbols_for_article` parameter names/types against `app/services/news_entity_matcher.py` (the call mirrors `query_service.get_symbol_news_mapping`, which uses `title=/summary=/keywords=/market=`). If `keywords` requires a tuple, pass `tuple(article.keywords or ())`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -k compute_mapped_symbols -v`
Expected: PASS (4 tests). (Depends on `삼성전자→005930` in the KR alias dict — confirmed present.)

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_market_news_symbol_enrich.py
git commit -m "feat(ROB-398): compute_mapped_symbols (persisted + live NER) for market news"
```

---

## Task 3: `_article_to_dict` + `_briefing_sections_to_dict` `mapped_by_id` enrich

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py`
- Test: `tests/test_market_news_symbol_enrich.py`

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.unit
def test_article_to_dict_includes_mapped_symbols():
    article = NewsArticle(
        id=5, market="kr", url="https://x/5", title="t", source="s",
        feed_source="f", summary=None, article_published_at=None,
        keywords=None, stock_symbol=None, stock_name=None,
    )
    mapped_by_id = {
        5: [{"symbol": "035420", "market": "kr", "mapping_source": "ner",
             "confidence": 0.5, "is_primary": True, "matched_term": "네이버"}]
    }
    item = nh._article_to_dict(article, mapped_by_id=mapped_by_id)
    assert item["mapped_symbols"] == mapped_by_id[5]


@pytest.mark.unit
def test_article_to_dict_mapped_symbols_defaults_empty():
    article = NewsArticle(
        id=6, market="kr", url="https://x/6", title="t", source="s",
        feed_source="f", summary=None, article_published_at=None,
        keywords=None, stock_symbol=None, stock_name=None,
    )
    item = nh._article_to_dict(article)
    assert item["mapped_symbols"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -k article_to_dict -v`
Expected: FAIL — `_article_to_dict()` has no `mapped_by_id` kwarg / no `mapped_symbols` key.

- [ ] **Step 3: Add the `mapped_by_id` parameter**

In `_article_to_dict`, change the signature and add the field:

```python
def _article_to_dict(
    article: NewsArticle,
    *,
    include_crypto_relevance: bool = False,
    briefing_relevance: dict[str, Any] | None = None,
    mapped_by_id: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    item = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "feed_source": article.feed_source,
        "market": article.market,
        "summary": article.summary,
        "published_at": article.article_published_at.isoformat()
        if article.article_published_at
        else None,
        "keywords": article.keywords,
        "stock_symbol": article.stock_symbol,
        "stock_name": article.stock_name,
        "mapped_symbols": (mapped_by_id or {}).get(article.id, []),
    }
    if include_crypto_relevance:
        item["crypto_relevance"] = score_crypto_news_article(article).as_dict()
    if briefing_relevance is not None:
        item["briefing_relevance"] = briefing_relevance
    return item
```

In `_briefing_sections_to_dict`, thread `mapped_by_id` to its inner `_article_to_dict` call:

```python
def _briefing_sections_to_dict(
    sections: list[BriefingSection],
    *,
    include_crypto_relevance: bool = False,
    mapped_by_id: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "section_id": section.section_id,
            "title": section.title,
            "count": len(section.items),
            "items": [
                _article_to_dict(
                    item.article,
                    include_crypto_relevance=include_crypto_relevance,
                    briefing_relevance=item.relevance.as_dict(),
                    mapped_by_id=mapped_by_id,
                )
                for item in section.items
            ],
        }
        for section in sections
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -k article_to_dict -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_market_news_symbol_enrich.py
git commit -m "feat(ROB-398): _article_to_dict/_briefing_sections_to_dict mapped_by_id enrich"
```

---

## Task 4: 시임 — `_get_market_news_impl` 배치 로드 + 스레딩 (+ e2e)

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py` (`_get_market_news_impl`)
- Test: `tests/test_market_news_symbol_enrich.py`

- [ ] **Step 1: Write the failing test** (append)

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_market_news_enriches_mainnews_via_ner(monkeypatch):
    a1 = NewsArticle(
        id=1, market="kr", url="https://x/1", title="삼성전자 신고가 경신",
        source="조선비즈", feed_source="browser_naver_mainnews", summary=None,
        article_published_at=None, keywords=None, stock_symbol=None, stock_name=None,
    )

    async def fake_get_news_articles(**kwargs):
        return [a1], 1

    async def fake_loader(article_ids):
        return {}  # mainnews: no persisted related rows

    monkeypatch.setattr(nh, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(nh, "load_related_rows_by_article_ids", fake_loader)

    resp = await nh._get_market_news_impl(market="kr", hours=24, limit=10)
    item = resp["news"][0]
    assert any(m["symbol"] == "005930" for m in item["mapped_symbols"])  # NER mapped despite no persisted rows
    # existing fields preserved
    assert item["stock_symbol"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_market_news_uses_persisted_related_rows(monkeypatch):
    from app.services.kr_news_symbol_mapping.contract import CandidateRow

    a1 = NewsArticle(
        id=7, market="kr", url="https://x/7", title="증시 코멘트", source="s",
        feed_source="f", summary=None, article_published_at=None, keywords=None,
        stock_symbol=None, stock_name=None,
    )

    async def fake_get_news_articles(**kwargs):
        return [a1], 1

    async def fake_loader(article_ids):
        return {7: (CandidateRow(symbol="000660", source="candidate", score=0.8),)}

    monkeypatch.setattr(nh, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(nh, "load_related_rows_by_article_ids", fake_loader)

    resp = await nh._get_market_news_impl(market="kr", hours=24, limit=10)
    syms = resp["news"][0]["mapped_symbols"]
    assert any(m["symbol"] == "000660" and m["mapping_source"] == "candidate" for m in syms)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -k get_market_news -v`
Expected: FAIL — `KeyError: 'mapped_symbols'` (the plain-branch `_article_to_dict(a)` call doesn't pass `mapped_by_id` yet, so it defaults to `[]` — assertion on `005930` fails).

- [ ] **Step 3: Add batch load + thread `mapped_by_id` to all 8 call sites**

In `_get_market_news_impl`, immediately after the `articles, total = await get_news_articles(...)` block (≈line 98), insert:

```python
    related_by_id = await load_related_rows_by_article_ids([a.id for a in articles])
    mapped_by_id: dict[int, list[dict[str, Any]]] = {
        a.id: compute_mapped_symbols(a, related_by_id.get(a.id, ()))
        for a in articles
    }
```

Then add `mapped_by_id=mapped_by_id` to every article-dict construction in the function. The 8 call sites become:

```python
    if market == "crypto":
        if briefing_filter:
            ranking = rank_crypto_news_for_briefing(list(articles), limit=limit)
            news_list = [
                _article_to_dict(item.article, include_crypto_relevance=True,
                                 mapped_by_id=mapped_by_id)
                for item in ranking.included
            ]
            excluded_news = [
                _article_to_dict(item.article, include_crypto_relevance=True,
                                 mapped_by_id=mapped_by_id)
                for item in ranking.excluded
            ]
            briefing_summary = ranking.summary
            briefing = format_market_news_briefing(
                list(articles), market=market, limit=limit
            )
            briefing_sections = _briefing_sections_to_dict(
                briefing.sections, include_crypto_relevance=True,
                mapped_by_id=mapped_by_id,
            )
        else:
            news_list = [
                _article_to_dict(a, include_crypto_relevance=True,
                                 mapped_by_id=mapped_by_id)
                for a in articles
            ]
    elif briefing_filter and market in {"us", "kr"}:
        briefing = format_market_news_briefing(
            list(articles), market=market, limit=limit
        )
        news_list = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
                mapped_by_id=mapped_by_id,
            )
            for section in briefing.sections
            for item in section.items
        ]
        excluded_news = [
            _article_to_dict(
                item.article,
                briefing_relevance=item.relevance.as_dict(),
                mapped_by_id=mapped_by_id,
            )
            for item in briefing.excluded
        ]
        briefing_summary = briefing.summary
        briefing_sections = _briefing_sections_to_dict(
            briefing.sections, mapped_by_id=mapped_by_id
        )
    else:
        news_list = [_article_to_dict(a, mapped_by_id=mapped_by_id) for a in articles]
```

(The rest of the function — `source_names`, the return dict — is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_market_news_symbol_enrich.py -v`
Expected: all PASS.

- [ ] **Step 5: Full slice + lint + news regression**

Run: `uv run pytest tests/test_kr_news_symbol_mapping_related_lookup.py tests/test_market_news_symbol_enrich.py -v`
Expected: all PASS.
Run: `uv run pytest tests/ -k "news or market_news or news_handlers or briefing" -q`
Expected: no failures introduced (existing `_article_to_dict`/`get_market_news` tests still pass — `mapped_symbols` is additive; if any test asserts the EXACT article-dict keyset, update it to include `mapped_symbols`).
Run: `uv run ruff check app/services/kr_news_symbol_mapping/related_lookup.py app/mcp_server/tooling/news_handlers.py tests/test_kr_news_symbol_mapping_related_lookup.py tests/test_market_news_symbol_enrich.py`
Expected: `All checks passed!` (run `ruff format` on the same paths if needed).

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_market_news_symbol_enrich.py
git commit -m "feat(ROB-398): wire mapped_symbols into get_market_news (all branches)"
```

---

## Self-Review

**1. Spec coverage:**
- §3 D1 각 기사 mapped_symbols → Task 3 (`_article_to_dict`) + Task 4 (스레딩). ✓
- §3 D2 독립 + 공유 배치 로더 → Task 1 (`related_lookup.py`). ✓
- §3 D3 full resolve(persisted + NER) → Task 2 (`compute_mapped_symbols`). ✓
- §3 D4 scope=get_market_news only → Task 4 (다른 도구 미변경). ✓
- §5 detached-safe → Task 1 (배치 로더, `a.related_symbols` 미사용) + Task 4 (scalar만 접근). ✓
- §6 응답 mapped_symbols → Task 3. ✓
- §7 정직성(매핑 0→[], confidence/is_primary 파생) → Task 2/3 tests. ✓
- §8 테스트(로더/compute/enrich/e2e/mainnews) → Tasks 1–4 tests. ✓
- §9 비범위(search_news/get_news/PR1 db_provider 미변경) → 미접근. migration 0. ✓

**2. Placeholder scan:** No TBD/TODO. The `match_symbols_for_article` param verification (Task 2) and the exact-keyset regression note (Task 4) are explicit instructions, not placeholders. ✓

**3. Type consistency:** `load_related_rows_by_article_ids` / `_group_rows` return `dict[int, tuple[CandidateRow, ...]]` (Task 1), consumed by Task 4 (`related_by_id.get(a.id, ())`) and passed to `compute_mapped_symbols(article, related_rows: tuple[CandidateRow, ...])` (Task 2). `compute_mapped_symbols` returns `list[dict]`, stored in `mapped_by_id: dict[int, list[dict]]` (Task 4), consumed by `_article_to_dict(mapped_by_id=...)` (Task 3). `_mapped_symbol_to_dict` keys match the response shape across tasks. ✓

**Open verification flags for the implementer (resolve while implementing, do not guess):**
- Exact `match_symbols_for_article` signature/keyword names + whether `keywords` wants list vs tuple (Task 2 note) — mirror `query_service.get_symbol_news_mapping`.
- In-memory `NewsArticle(...)`/`NewsArticleRelatedSymbol(...)` construction without flush (tests) — if the test DB plugin auto-flushes, use `SimpleNamespace` stand-ins exposing the same read attributes.
- Any existing test asserting the EXACT `_article_to_dict` keyset or `get_market_news` news-item keys must add `mapped_symbols` (Task 4 Step 5).

---

## Execution Handoff

Plan complete. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, two-stage review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
