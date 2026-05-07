# ROB-130: News Issue Clustering & Ticker News Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, read-only foundation that (1) maps `news_articles` rows to KR/US/crypto symbols via an entity matcher, (2) enables ticker research sessions (e.g., AMZN, 005930) to surface relevant collected news even when `stock_symbol` is null, and (3) groups recent articles into ranked market-issue clusters consumable by `/invest/app` and MCP — without Toss APIs, broker side-effects, or LLM-powered article summarization.

**Architecture:** Three layers. (1) `NewsEntityMatcher` service — alias dictionaries (KR/US/crypto) + DB symbol universe + existing `stock_aliases` table — exposes a pure function `match_symbols(text, market) -> list[SymbolMatch]` with reason/confidence. (2) Extend `get_news_articles` with a multi-stage fallback (`exact → candidate metadata → alias match → market high-signal`) used by `news_stage._fetch_recent_headlines`. (3) `NewsIssueClusteringService` builds deterministic clusters by entity overlap + normalized-title shingles + source diversity over a configurable time window, returning stable hash-derived issue IDs. A new read-only router `/trading/api/news-issues` and an optional MCP tool expose the clusters; existing `/trading/api/news-radar`, `get_market_news`, and preopen consumers stay backward compatible.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async (asyncpg), PostgreSQL JSONB, pydantic v2, pytest with `pytest-asyncio` (strict). No new runtime deps; reuses `app.core.timezone`, `app.services.llm_news_service`, `app.services.stock_alias_service`, KR/US/Upbit symbol universe services.

---

## File Structure

**Created (new files):**
- `app/services/news_entity_matcher.py` — entity matcher service (alias dict + DB lookups + match reasons)
- `app/services/news_entity_alias_data.py` — built-in KR/US/crypto canonical alias dictionaries (data only)
- `app/services/news_issue_clustering_service.py` — issue clustering MVP and ranking
- `app/schemas/news_issues.py` — pydantic schemas (`MarketIssue`, `MarketIssueArticle`, `IssueSignals`, `MarketIssuesResponse`)
- `app/routers/news_issues.py` — read-only router `/trading/api/news-issues`
- `tests/test_news_entity_matcher.py` — alias matching unit tests
- `tests/test_news_issue_clustering.py` — clustering/ranking unit tests with deterministic fixtures
- `tests/test_news_stage_fallback.py` — research-session fallback behaviour
- `tests/test_router_news_issues.py` — API contract test

**Modified:**
- `app/services/llm_news_service.py` — add `get_news_articles_with_fallback(...)` (does **not** change `get_news_articles` semantics)
- `app/analysis/stages/news_stage.py` — `_fetch_recent_headlines` uses the new fallback function
- `app/mcp_server/tooling/news_handlers.py` — add optional `get_market_issues` MCP tool (read-only)
- `app/main.py` — register `news_issues_router`
- `docs/runbooks/news-issue-clustering.md` — runbook for the new endpoint and how it consumes ROB-129 metadata when available (created in Task 11)

**Why this split:** alias dictionaries are pure data and change frequently → isolated from matcher logic. Clustering is its own service so the router stays thin. Fallback lookup lives in `llm_news_service` next to `get_news_articles` so all news-query call sites can adopt it later. No DB migration is needed for the MVP — `news_articles.candidate_symbols`/`candidate_sectors` JSONB columns are deferred to ROB-129; the matcher reads optional `keywords` JSONB and computes candidates on the fly.

---

## Task 1: Built-in alias dictionaries (data file)

**Files:**
- Create: `app/services/news_entity_alias_data.py`

- [ ] **Step 1: Create the alias-dictionary module**

```python
# app/services/news_entity_alias_data.py
"""Built-in deterministic alias dictionaries for the news entity matcher (ROB-130).

Data-only module. Keep entries narrow and high-signal. Each entry is a (symbol,
market, canonical_name, alias_terms) tuple. `alias_terms` are matched
case-insensitively against title + summary + joined keywords. Korean terms are
matched as substrings; English terms are matched on word boundaries.

These dictionaries are intentionally a small, high-precision set covering the
acceptance-criteria examples (AMZN, 005930, BTC) plus the most-traded peers.
Long-tail mapping is delegated to the DB symbol universe + `stock_aliases`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AliasEntry:
    symbol: str          # canonical DB form (e.g. "005930", "AMZN", "BTC")
    market: str          # "kr" | "us" | "crypto"
    canonical_name: str  # display name
    aliases: tuple[str, ...]  # case-insensitive substring/word-boundary terms


KR_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("005930", "kr", "삼성전자", ("삼성전자", "삼전", "Samsung Electronics")),
    AliasEntry("000660", "kr", "SK하이닉스", ("SK하이닉스", "하이닉스", "닉스", "SK Hynix")),
    AliasEntry("035420", "kr", "NAVER", ("네이버", "NAVER")),
    AliasEntry("035720", "kr", "카카오", ("카카오",)),
    AliasEntry("323410", "kr", "카카오뱅크", ("카카오뱅크",)),
    AliasEntry("377300", "kr", "카카오페이", ("카카오페이",)),
    AliasEntry("207940", "kr", "삼성바이오로직스", ("삼성바이오", "삼성바이오로직스")),
    AliasEntry("005380", "kr", "현대차", ("현대차", "현대자동차", "Hyundai Motor")),
    AliasEntry("005490", "kr", "POSCO홀딩스", ("POSCO", "포스코")),
    AliasEntry("373220", "kr", "LG에너지솔루션", ("LG에너지솔루션", "LG엔솔")),
)

US_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("AAPL", "us", "Apple", ("Apple", "AAPL", "애플")),
    AliasEntry("AMZN", "us", "Amazon", ("Amazon", "AMZN", "아마존")),
    AliasEntry("NVDA", "us", "Nvidia", ("Nvidia", "NVDA", "엔비디아")),
    AliasEntry("TSLA", "us", "Tesla", ("Tesla", "TSLA", "테슬라")),
    AliasEntry("META", "us", "Meta", ("Meta Platforms", "META", "메타")),
    AliasEntry("GOOGL", "us", "Alphabet", ("Alphabet", "Google", "GOOGL", "GOOG", "구글")),
    AliasEntry("MSFT", "us", "Microsoft", ("Microsoft", "MSFT", "마이크로소프트")),
    AliasEntry("AMD", "us", "AMD", ("AMD", "Advanced Micro")),
    AliasEntry("AVGO", "us", "Broadcom", ("Broadcom", "AVGO")),
    AliasEntry("BRK.B", "us", "Berkshire Hathaway B", ("Berkshire Hathaway",)),
)

CRYPTO_ALIASES: tuple[AliasEntry, ...] = (
    AliasEntry("BTC", "crypto", "Bitcoin", ("Bitcoin", "BTC", "비트코인", "KRW-BTC")),
    AliasEntry("ETH", "crypto", "Ethereum", ("Ethereum", "ETH", "이더리움", "KRW-ETH")),
    AliasEntry("SOL", "crypto", "Solana", ("Solana", "SOL", "솔라나", "KRW-SOL")),
    AliasEntry("XRP", "crypto", "Ripple", ("Ripple", "XRP", "리플", "KRW-XRP")),
    AliasEntry("DOGE", "crypto", "Dogecoin", ("Dogecoin", "DOGE", "도지코인", "KRW-DOGE")),
)

ALL_ALIASES: tuple[AliasEntry, ...] = KR_ALIASES + US_ALIASES + CRYPTO_ALIASES
```

- [ ] **Step 2: Commit**

```bash
git add app/services/news_entity_alias_data.py
git commit -m "feat(news): add built-in alias dictionaries for entity matcher (ROB-130)"
```

---

## Task 2: Entity matcher — failing tests first

**Files:**
- Test: `tests/test_news_entity_matcher.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_entity_matcher.py
"""Unit tests for the deterministic news entity matcher (ROB-130)."""

from __future__ import annotations

import pytest

from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols,
    match_symbols_for_article,
)


@pytest.mark.unit
def test_us_amazon_alias_matches_amzn():
    matches = match_symbols("Amazon raises guidance on AWS demand", market="us")
    symbols = [m.symbol for m in matches]
    assert "AMZN" in symbols
    amzn = next(m for m in matches if m.symbol == "AMZN")
    assert amzn.market == "us"
    assert amzn.reason == "alias_dict"
    assert amzn.matched_term.lower() == "amazon"


@pytest.mark.unit
def test_us_ticker_uppercase_matches():
    matches = match_symbols("AMZN options skew flips bullish", market="us")
    assert any(m.symbol == "AMZN" for m in matches)


@pytest.mark.unit
def test_kr_samsung_korean_alias_matches_005930():
    matches = match_symbols("삼성전자 1분기 실적 호조, 삼전 강세", market="kr")
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_kr_samjeon_short_alias_matches():
    matches = match_symbols("삼전 매수 우위", market="kr")
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_crypto_bitcoin_alias_matches_btc():
    matches = match_symbols("비트코인 7만달러 회복, Bitcoin ETF 유입 지속", market="crypto")
    symbols = [m.symbol for m in matches]
    assert "BTC" in symbols


@pytest.mark.unit
def test_crypto_krw_pair_matches_btc():
    matches = match_symbols("KRW-BTC 거래대금 급증", market="crypto")
    assert any(m.symbol == "BTC" for m in matches)


@pytest.mark.unit
def test_market_filter_excludes_other_markets():
    matches = match_symbols("Amazon, 삼성전자 모두 강세", market="us")
    symbols = {m.symbol for m in matches}
    assert "AMZN" in symbols
    assert "005930" not in symbols  # market=us must filter KR


@pytest.mark.unit
def test_no_match_returns_empty_list():
    assert match_symbols("Random unrelated content about weather", market="us") == []


@pytest.mark.unit
def test_us_word_boundary_no_false_positive_for_amd_in_amid():
    # "amid" must NOT match "AMD"
    matches = match_symbols("Stocks rally amid easing inflation", market="us")
    assert not any(m.symbol == "AMD" for m in matches)


@pytest.mark.unit
def test_match_for_article_uses_title_summary_keywords():
    matches = match_symbols_for_article(
        title="실적발표",
        summary=None,
        keywords=["삼성전자", "반도체"],
        market="kr",
    )
    assert any(m.symbol == "005930" for m in matches)


@pytest.mark.unit
def test_match_returns_sorted_unique_by_symbol():
    matches = match_symbols("Amazon Amazon AMZN keeps rising", market="us")
    amzn_matches = [m for m in matches if m.symbol == "AMZN"]
    assert len(amzn_matches) == 1  # deduped
    assert isinstance(matches[0], SymbolMatch)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_news_entity_matcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.news_entity_matcher'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_news_entity_matcher.py
git commit -m "test(news): add failing tests for news entity matcher (ROB-130)"
```

---

## Task 3: Entity matcher — implementation

**Files:**
- Create: `app/services/news_entity_matcher.py`

- [ ] **Step 1: Implement the matcher to make Task 2 tests pass**

```python
# app/services/news_entity_matcher.py
"""Deterministic news entity matcher (ROB-130).

Pure functions over the built-in alias dictionaries. Future enhancement (ROB-129):
when `news_articles` carry candidate metadata from the news-ingestor pipeline,
prefer those. Until then, this module is the only entity-tagging layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.services.news_entity_alias_data import (
    ALL_ALIASES,
    AliasEntry,
    CRYPTO_ALIASES,
    KR_ALIASES,
    US_ALIASES,
)

_ASCII_WORDISH = re.compile(r"[A-Za-z0-9]")


@dataclass(frozen=True)
class SymbolMatch:
    symbol: str
    market: str
    canonical_name: str
    matched_term: str
    reason: str  # "alias_dict" | "candidate_metadata" | "exact_symbol"


def _aliases_for_market(market: str | None) -> tuple[AliasEntry, ...]:
    if market == "kr":
        return KR_ALIASES
    if market == "us":
        return US_ALIASES
    if market == "crypto":
        return CRYPTO_ALIASES
    return ALL_ALIASES


def _is_ascii_term(term: str) -> bool:
    return bool(term) and all(ord(c) < 128 for c in term)


def _term_matches(haystack_lower: str, term: str) -> bool:
    """Korean/non-ASCII → substring match.
    ASCII (English/ticker) → word-boundary match to avoid 'AMD' in 'amid'.
    """
    if not term:
        return False
    needle = term.lower()
    if not _is_ascii_term(term):
        return needle in haystack_lower
    pattern = r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])"
    return re.search(pattern, haystack_lower) is not None


def match_symbols(
    text: str,
    *,
    market: str | None = None,
) -> list[SymbolMatch]:
    """Return symbol matches found in `text`, deduped by symbol.

    Sorted deterministically by (market, symbol). Empty list when no matches.
    """
    if not text:
        return []
    haystack = text.lower()
    candidates = _aliases_for_market(market)
    seen: dict[str, SymbolMatch] = {}
    for entry in candidates:
        for alias in entry.aliases:
            if _term_matches(haystack, alias):
                if entry.symbol not in seen:
                    seen[entry.symbol] = SymbolMatch(
                        symbol=entry.symbol,
                        market=entry.market,
                        canonical_name=entry.canonical_name,
                        matched_term=alias,
                        reason="alias_dict",
                    )
                break  # first alias hit per symbol is enough
    return sorted(seen.values(), key=lambda m: (m.market, m.symbol))


def match_symbols_for_article(
    *,
    title: str | None,
    summary: str | None = None,
    keywords: Iterable[str] | None = None,
    market: str | None = None,
) -> list[SymbolMatch]:
    """Convenience wrapper: combine article fields then call `match_symbols`."""
    parts: list[str] = []
    if title:
        parts.append(title)
    if summary:
        parts.append(summary)
    if keywords:
        parts.append(" ".join(str(k) for k in keywords if k))
    return match_symbols(" \n ".join(parts), market=market)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_news_entity_matcher.py -v`
Expected: 11 passed

- [ ] **Step 3: Commit**

```bash
git add app/services/news_entity_matcher.py
git commit -m "feat(news): deterministic entity matcher with KR/US/crypto aliases (ROB-130)"
```

---

## Task 4: Ticker news fallback — failing tests

**Files:**
- Test: `tests/test_news_stage_fallback.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_stage_fallback.py
"""Tests for ticker research-session news fallback (ROB-130).

Verifies that when `news_articles.stock_symbol` is null but title/summary
contain a known alias, the fallback returns those rows tagged with a match
reason — instead of an empty list.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.timezone import now_kst_naive
from app.services import llm_news_service


def _mk_article(
    *,
    id: int,
    title: str,
    summary: str | None = None,
    stock_symbol: str | None = None,
    market: str = "us",
    published_minutes_ago: int = 60,
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        title=title,
        summary=summary,
        stock_symbol=stock_symbol,
        stock_name=None,
        market=market,
        keywords=keywords or [],
        article_published_at=now_kst_naive() - timedelta(minutes=published_minutes_ago),
        url=f"https://example.com/{id}",
        source="example",
        feed_source="rss_test",
    )


@pytest.mark.unit
async def test_fallback_exact_symbol_match_returned_first(monkeypatch):
    exact = [_mk_article(id=1, title="AMZN beats", stock_symbol="AMZN")]

    async def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "AMZN":
            return exact, len(exact)
        return [], 0

    monkeypatch.setattr(
        llm_news_service, "get_news_articles", AsyncMock(side_effect=fake_get_news_articles)
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    assert len(result.articles) == 1
    assert result.articles[0].id == 1
    assert result.match_reasons[1] == "exact_symbol"


@pytest.mark.unit
async def test_fallback_alias_used_when_exact_returns_empty(monkeypatch):
    untagged = [
        _mk_article(id=10, title="Amazon raises guidance on AWS", stock_symbol=None),
        _mk_article(id=11, title="Apple reports Q1", stock_symbol=None),
    ]

    async def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "AMZN":
            return [], 0
        # market-wide query (no stock_symbol)
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service, "get_news_articles", AsyncMock(side_effect=fake_get_news_articles)
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    ids = [a.id for a in result.articles]
    assert 10 in ids
    assert 11 not in ids  # Apple article must not match AMZN
    assert result.match_reasons[10] == "alias_match"


@pytest.mark.unit
async def test_fallback_kr_005930_alias_match(monkeypatch):
    untagged = [_mk_article(id=20, title="삼성전자 1분기 실적 호조", stock_symbol=None, market="kr")]

    async def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol") == "005930":
            return [], 0
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service, "get_news_articles", AsyncMock(side_effect=fake_get_news_articles)
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="005930", market="kr", hours=24, limit=20
    )
    assert any(a.id == 20 for a in result.articles)
    assert result.match_reasons[20] == "alias_match"


@pytest.mark.unit
async def test_fallback_returns_empty_when_no_match(monkeypatch):
    async def fake_get_news_articles(**kwargs):
        return [], 0

    monkeypatch.setattr(
        llm_news_service, "get_news_articles", AsyncMock(side_effect=fake_get_news_articles)
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=20
    )
    assert result.articles == []
    assert result.match_reasons == {}


@pytest.mark.unit
async def test_fallback_caps_limit(monkeypatch):
    untagged = [_mk_article(id=i, title="Amazon news", stock_symbol=None) for i in range(50)]

    async def fake_get_news_articles(**kwargs):
        if kwargs.get("stock_symbol"):
            return [], 0
        return untagged, len(untagged)

    monkeypatch.setattr(
        llm_news_service, "get_news_articles", AsyncMock(side_effect=fake_get_news_articles)
    )

    result = await llm_news_service.get_news_articles_with_fallback(
        symbol="AMZN", market="us", hours=24, limit=5
    )
    assert len(result.articles) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_news_stage_fallback.py -v`
Expected: FAIL with `AttributeError: module 'app.services.llm_news_service' has no attribute 'get_news_articles_with_fallback'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_news_stage_fallback.py
git commit -m "test(news): add failing tests for ticker news fallback (ROB-130)"
```

---

## Task 5: Ticker news fallback — implementation

**Files:**
- Modify: `app/services/llm_news_service.py`

- [ ] **Step 1: Add the fallback function near `get_news_articles`**

Append to `app/services/llm_news_service.py` (after the existing `get_news_articles` definition, ~line 190). Do not change `get_news_articles`.

```python
from dataclasses import dataclass, field

from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols_for_article,
)


@dataclass
class NewsLookupResult:
    """Result of a ticker news lookup with fallback reasoning."""

    articles: list[NewsArticle]
    match_reasons: dict[int, str] = field(default_factory=dict)  # article.id -> reason


async def get_news_articles_with_fallback(
    *,
    symbol: str,
    market: str,
    hours: int = 24,
    limit: int = 20,
) -> NewsLookupResult:
    """Ticker research news lookup with deterministic fallback.

    Strategy:
      1. exact stock_symbol rows
      2. (future ROB-129) candidate metadata rows — currently a no-op
      3. alias title/summary/keywords match over recent market rows

    Returns a `NewsLookupResult` with `match_reasons[article.id]` set to one of:
    "exact_symbol" | "alias_match".
    """
    exact_articles, _ = await get_news_articles(
        stock_symbol=symbol, market=market, hours=hours, limit=limit
    )
    seen_ids: set[int] = set()
    out: list[NewsArticle] = []
    reasons: dict[int, str] = {}

    for art in exact_articles:
        if art.id in seen_ids:
            continue
        seen_ids.add(art.id)
        reasons[art.id] = "exact_symbol"
        out.append(art)
        if len(out) >= limit:
            return NewsLookupResult(articles=out, match_reasons=reasons)

    # Step 3: alias fallback over a wider market window.
    market_articles, _ = await get_news_articles(
        market=market, hours=hours, limit=max(limit * 5, 50)
    )
    target_symbol = symbol.upper().strip()
    for art in market_articles:
        if art.id in seen_ids:
            continue
        matches: list[SymbolMatch] = match_symbols_for_article(
            title=art.title,
            summary=getattr(art, "summary", None),
            keywords=getattr(art, "keywords", None) or [],
            market=market,
        )
        if any(m.symbol.upper() == target_symbol for m in matches):
            seen_ids.add(art.id)
            reasons[art.id] = "alias_match"
            out.append(art)
            if len(out) >= limit:
                break

    return NewsLookupResult(articles=out, match_reasons=reasons)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_news_stage_fallback.py -v`
Expected: 5 passed

- [ ] **Step 3: Commit**

```bash
git add app/services/llm_news_service.py
git commit -m "feat(news): add get_news_articles_with_fallback for ticker lookup (ROB-130)"
```

---

## Task 6: Wire fallback into `news_stage` (research session)

**Files:**
- Modify: `app/analysis/stages/news_stage.py:96-134`

- [ ] **Step 1: Update `_fetch_recent_headlines` to use the fallback**

In `app/analysis/stages/news_stage.py`, replace the body of `_fetch_recent_headlines` (the function spanning roughly lines 96–134). Keep the on-demand provider fetch path unchanged but switch the DB query to the fallback function:

```python
async def _fetch_recent_headlines(
    symbol: str,
    instrument_type: str,
    *,
    stock_name: str | None,
) -> dict[str, Any]:
    """Fetch recent headlines, augmenting DB with on-demand provider fetch
    when symbol-tagged news is below threshold."""
    market = _market_from_instrument(instrument_type)

    lookup = await get_news_articles_with_fallback(
        symbol=symbol, market=market, hours=24, limit=20
    )
    articles = lookup.articles

    if len(articles) < MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH:
        fetched = await fetch_symbol_news(symbol, instrument_type, limit=20)
        if fetched:
            payloads = _to_persist_payloads(
                fetched, symbol=symbol, stock_name=stock_name, market=market
            )
            try:
                await bulk_create_news_articles(payloads)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "news_stage: bulk_create_news_articles failed: symbol=%s err=%s",
                    symbol,
                    exc,
                )
            lookup = await get_news_articles_with_fallback(
                symbol=symbol, market=market, hours=24, limit=20
            )
            articles = lookup.articles
            if not articles:
                logger.info(
                    "news_stage: using fetched headlines as signal fallback: symbol=%s",
                    symbol,
                )
                return _compute_signals_from_articles(_to_signal_articles(fetched))

    return _compute_signals_from_articles(articles)
```

Also update the import at the top of the file (around line 14) to add the new symbol:

```python
from app.services.llm_news_service import (
    bulk_create_news_articles,
    get_news_articles,
    get_news_articles_with_fallback,
)
```

- [ ] **Step 2: Run the existing news_stage tests to confirm no regression**

Run: `uv run pytest tests/test_news_stage_on_demand.py -v`
Expected: all passing (the fallback wrapper now intermediates the DB query but preserves prior behaviour for symbol-tagged rows)

- [ ] **Step 3: Run the fallback tests too**

Run: `uv run pytest tests/test_news_stage_fallback.py tests/test_news_entity_matcher.py -v`
Expected: 16 passed

- [ ] **Step 4: Commit**

```bash
git add app/analysis/stages/news_stage.py
git commit -m "feat(news): research session uses fallback lookup for ticker headlines (ROB-130)"
```

---

## Task 7: Issue clustering schemas — pydantic contract

**Files:**
- Create: `app/schemas/news_issues.py`

- [ ] **Step 1: Define the read-only schemas**

```python
# app/schemas/news_issues.py
"""Pydantic schemas for the market issue clustering read-only API (ROB-130)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketIssueMarket = Literal["kr", "us", "crypto"]
IssueDirection = Literal["up", "down", "mixed", "neutral"]


class IssueSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recency_score: float = Field(ge=0.0, le=1.0)
    source_diversity_score: float = Field(ge=0.0, le=1.0)
    mention_score: float = Field(ge=0.0, le=1.0)


class MarketIssueArticle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    url: str
    source: str | None
    feed_source: str | None
    published_at: datetime | None
    summary: str | None = None
    matched_terms: list[str] = Field(default_factory=list)


class MarketIssueRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    canonical_name: str
    mention_count: int = 0


class MarketIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    market: MarketIssueMarket
    rank: int
    issue_title: str
    subtitle: str | None
    direction: IssueDirection
    source_count: int
    article_count: int
    updated_at: datetime
    summary: str | None = None
    related_symbols: list[MarketIssueRelatedSymbol] = Field(default_factory=list)
    related_sectors: list[str] = Field(default_factory=list)
    articles: list[MarketIssueArticle] = Field(default_factory=list)
    signals: IssueSignals


class MarketIssuesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketIssueMarket | Literal["all"]
    as_of: datetime
    window_hours: int
    items: list[MarketIssue] = Field(default_factory=list)
```

- [ ] **Step 2: Quick import smoke check**

Run: `uv run python -c "from app.schemas.news_issues import MarketIssue, MarketIssuesResponse; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add app/schemas/news_issues.py
git commit -m "feat(news): pydantic schemas for market issue clustering (ROB-130)"
```

---

## Task 8: Issue clustering service — failing tests first

**Files:**
- Test: `tests/test_news_issue_clustering.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_news_issue_clustering.py
"""Unit tests for the deterministic news issue clustering MVP (ROB-130)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.timezone import now_kst_naive
from app.services import news_issue_clustering_service as clustering


def _mk(*, id: int, title: str, source: str, summary: str = "",
        published_minutes_ago: int = 30, keywords: list[str] | None = None,
        market: str = "us"):
    return SimpleNamespace(
        id=id,
        title=title,
        summary=summary,
        source=source,
        feed_source=f"rss_{source}",
        url=f"https://example.com/{id}",
        market=market,
        keywords=keywords or [],
        article_published_at=now_kst_naive() - timedelta(minutes=published_minutes_ago),
        stock_symbol=None,
    )


@pytest.mark.unit
async def test_clusters_articles_sharing_amazon_entity(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand", source="cnbc"),
        _mk(id=2, title="AWS growth boosts Amazon outlook", source="bloomberg"),
        _mk(id=3, title="Apple reports record iPhone sales", source="reuters"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.market == "us"
    titles = [iss.issue_title for iss in result.items]
    assert any("Amazon" in t or "AMZN" in t for t in titles)

    amzn_issue = next(iss for iss in result.items if any(
        rs.symbol == "AMZN" for rs in iss.related_symbols))
    assert amzn_issue.article_count == 2
    assert amzn_issue.source_count == 2
    article_ids = {a.id for a in amzn_issue.articles}
    assert article_ids == {1, 2}


@pytest.mark.unit
async def test_rank_orders_by_score_desc(monkeypatch):
    base = now_kst_naive()
    rows = [
        _mk(id=1, title="Amazon up", source="cnbc", published_minutes_ago=10),
        _mk(id=2, title="Amazon AWS", source="bloomberg", published_minutes_ago=15),
        _mk(id=3, title="Amazon retail", source="reuters", published_minutes_ago=20),
        _mk(id=4, title="Tesla recall report", source="cnbc", published_minutes_ago=180),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.items[0].rank == 1
    # Amazon issue (3 articles, 3 sources, fresh) must outrank Tesla (1 article)
    assert any(rs.symbol == "AMZN" for rs in result.items[0].related_symbols)


@pytest.mark.unit
async def test_returns_empty_when_no_articles(monkeypatch):
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=[])
    )
    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert result.items == []


@pytest.mark.unit
async def test_id_is_stable_for_same_input(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance", source="cnbc"),
        _mk(id=2, title="AWS demand boosts Amazon", source="bloomberg"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    first = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    second = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    assert [iss.id for iss in first.items] == [iss.id for iss in second.items]


@pytest.mark.unit
async def test_kr_clustering_groups_005930(monkeypatch):
    rows = [
        _mk(id=11, title="삼성전자 1분기 실적 호조", source="mk", market="kr"),
        _mk(id=12, title="삼전 어닝 서프라이즈", source="hankyung", market="kr"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(market="kr", window_hours=24, limit=10)
    assert any(
        any(rs.symbol == "005930" for rs in iss.related_symbols)
        for iss in result.items
    )


@pytest.mark.unit
async def test_signal_scores_are_in_unit_interval(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon up", source="cnbc"),
        _mk(id=2, title="Amazon up", source="bloomberg"),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )
    result = await clustering.build_market_issues(market="us", window_hours=24, limit=10)
    for iss in result.items:
        assert 0.0 <= iss.signals.recency_score <= 1.0
        assert 0.0 <= iss.signals.source_diversity_score <= 1.0
        assert 0.0 <= iss.signals.mention_score <= 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_news_issue_clustering.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.news_issue_clustering_service'`

- [ ] **Step 3: Commit failing tests**

```bash
git add tests/test_news_issue_clustering.py
git commit -m "test(news): add failing tests for issue clustering MVP (ROB-130)"
```

---

## Task 9: Issue clustering service — implementation

**Files:**
- Create: `app/services/news_issue_clustering_service.py`

- [ ] **Step 1: Implement the deterministic clustering service**

```python
# app/services/news_issue_clustering_service.py
"""Deterministic market-issue clustering MVP (ROB-130).

Read-only service. Groups recent articles by:
  1. Shared entity matches (alias dictionary)
  2. Title shingles (3-grams of normalized words) when no shared entity exists
Output is a stable, ranked list of `MarketIssue` objects.

Future LLM-powered impact summarization can replace `_pick_issue_title`/
`_pick_subtitle` without changing the contract.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst_naive
from app.models.news import NewsArticle
from app.schemas.news_issues import (
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssueRelatedSymbol,
    MarketIssuesResponse,
)
from app.services.news_entity_matcher import (
    SymbolMatch,
    match_symbols_for_article,
)

_WORD_RE = re.compile(r"[A-Za-z0-9가-힣]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "as", "at", "by", "with", "from", "이", "그", "저", "및", "는", "이는", "관련",
}


async def _load_recent_articles(
    *, market: str | None, window_hours: int, max_rows: int
) -> list[NewsArticle]:
    cutoff = now_kst_naive() - timedelta(hours=window_hours)
    async with AsyncSessionLocal() as db:
        stmt = (
            select(NewsArticle)
            .where(NewsArticle.article_published_at.is_not(None))
            .where(NewsArticle.article_published_at >= cutoff)
        )
        if market is not None and market != "all":
            stmt = stmt.where(NewsArticle.market == market)
        stmt = stmt.order_by(NewsArticle.article_published_at.desc()).limit(max_rows)
        result = await db.execute(stmt)
        return list(result.scalars().all())


def _normalize_words(text: str) -> list[str]:
    return [w for w in (m.lower() for m in _WORD_RE.findall(text or "")) if w not in _STOPWORDS]


def _shingles(words: list[str], n: int = 3) -> set[tuple[str, ...]]:
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


@dataclass
class _Cluster:
    article_ids: list[int]
    article_indexes: list[int]
    matches: list[SymbolMatch]
    cluster_key: str  # symbol-or-shingle-derived stable key


def _cluster_articles(
    articles: list[NewsArticle], market: str
) -> list[_Cluster]:
    """Two-pass clustering:
       1. Group by primary entity match (first symbol per article).
       2. Articles without entity → group by shared shingles (Jaccard >= 0.34).
    """
    by_symbol: dict[str, _Cluster] = {}
    leftover_indexes: list[int] = []
    leftover_shingles: list[set[tuple[str, ...]]] = []
    leftover_words: list[list[str]] = []

    for idx, art in enumerate(articles):
        matches = match_symbols_for_article(
            title=art.title,
            summary=getattr(art, "summary", None),
            keywords=getattr(art, "keywords", None) or [],
            market=market if market != "all" else None,
        )
        if matches:
            primary = matches[0]
            cluster = by_symbol.setdefault(
                primary.symbol,
                _Cluster(
                    article_ids=[],
                    article_indexes=[],
                    matches=[],
                    cluster_key=f"sym:{primary.market}:{primary.symbol}",
                ),
            )
            cluster.article_ids.append(art.id)
            cluster.article_indexes.append(idx)
            for m in matches:
                if m not in cluster.matches:
                    cluster.matches.append(m)
        else:
            words = _normalize_words(f"{art.title} {getattr(art, 'summary', '') or ''}")
            leftover_indexes.append(idx)
            leftover_words.append(words)
            leftover_shingles.append(_shingles(words))

    clusters: list[_Cluster] = list(by_symbol.values())

    # Greedy shingle clustering for leftovers.
    used = [False] * len(leftover_indexes)
    for i, shingles_i in enumerate(leftover_shingles):
        if used[i] or not shingles_i:
            continue
        used[i] = True
        members = [i]
        for j in range(i + 1, len(leftover_shingles)):
            if used[j] or not leftover_shingles[j]:
                continue
            inter = len(shingles_i & leftover_shingles[j])
            union = len(shingles_i | leftover_shingles[j])
            if union and inter / union >= 0.34:
                used[j] = True
                members.append(j)
        rep_words = leftover_words[i][:6] or ["topic"]
        key = "shg:" + "_".join(rep_words[:3])
        cluster = _Cluster(
            article_ids=[articles[leftover_indexes[m]].id for m in members],
            article_indexes=[leftover_indexes[m] for m in members],
            matches=[],
            cluster_key=key,
        )
        clusters.append(cluster)

    return clusters


def _stable_id(market: str, cluster_key: str, article_ids: Iterable[int]) -> str:
    payload = f"{market}|{cluster_key}|" + ",".join(str(i) for i in sorted(article_ids))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _pick_issue_title(cluster: _Cluster, articles: list[NewsArticle]) -> str:
    if cluster.matches:
        return cluster.matches[0].canonical_name
    # Shortest title among the cluster's articles is usually the headline-y one.
    titles = [articles[i].title for i in cluster.article_indexes if articles[i].title]
    titles.sort(key=len)
    return titles[0] if titles else "Trending topic"


def _pick_subtitle(cluster: _Cluster, articles: list[NewsArticle]) -> str | None:
    titles = [articles[i].title for i in cluster.article_indexes]
    if len(titles) <= 1:
        return None
    return titles[1] if len(titles) > 1 else None


def _direction_from_titles(titles: list[str]) -> str:
    pos_words = ("rise", "raise", "beat", "surge", "rally", "up", "상승", "급등", "호재", "최고")
    neg_words = ("fall", "drop", "miss", "plunge", "down", "하락", "급락", "악재", "위기")
    pos = sum(1 for t in titles if any(w in t.lower() for w in pos_words))
    neg = sum(1 for t in titles if any(w in t.lower() for w in neg_words))
    if pos and not neg:
        return "up"
    if neg and not pos:
        return "down"
    if pos and neg:
        return "mixed"
    return "neutral"


def _signals(
    cluster: _Cluster, articles: list[NewsArticle], window_hours: int
) -> IssueSignals:
    if not cluster.article_indexes:
        return IssueSignals(recency_score=0.0, source_diversity_score=0.0, mention_score=0.0)

    now = now_kst_naive()
    ages = []
    for idx in cluster.article_indexes:
        pub = articles[idx].article_published_at
        if pub is not None:
            mins = max(0, int((now - pub.replace(tzinfo=None)).total_seconds() / 60))
            ages.append(mins)
    if not ages:
        recency = 0.0
    else:
        newest = min(ages)
        recency = max(0.0, 1.0 - newest / max(1, window_hours * 60))

    sources = {articles[i].source for i in cluster.article_indexes if articles[i].source}
    source_diversity = min(1.0, len(sources) / 5.0)

    mention = min(1.0, math.log1p(len(cluster.article_indexes)) / math.log(10))

    return IssueSignals(
        recency_score=round(recency, 3),
        source_diversity_score=round(source_diversity, 3),
        mention_score=round(mention, 3),
    )


def _to_market_issue(
    *,
    cluster: _Cluster,
    articles: list[NewsArticle],
    market: str,
    window_hours: int,
    rank: int,
) -> MarketIssue:
    indexes = cluster.article_indexes
    cluster_articles = [articles[i] for i in indexes]
    signals = _signals(cluster, articles, window_hours)
    direction = _direction_from_titles([a.title for a in cluster_articles])

    related_symbols = [
        MarketIssueRelatedSymbol(
            symbol=m.symbol,
            market=m.market,
            canonical_name=m.canonical_name,
            mention_count=sum(1 for a in cluster_articles if m.matched_term.lower() in (a.title or "").lower()),
        )
        for m in cluster.matches
    ]

    sources = {a.source for a in cluster_articles if a.source}
    updated_at = max(
        (a.article_published_at for a in cluster_articles if a.article_published_at),
        default=now_kst_naive(),
    )

    issue_articles = [
        MarketIssueArticle(
            id=a.id,
            title=a.title,
            url=a.url,
            source=a.source,
            feed_source=a.feed_source,
            published_at=a.article_published_at,
            summary=getattr(a, "summary", None),
            matched_terms=[m.matched_term for m in cluster.matches],
        )
        for a in cluster_articles
    ]

    issue_market = cluster_articles[0].market if cluster_articles else market
    if issue_market not in ("kr", "us", "crypto"):
        issue_market = "us"

    return MarketIssue(
        id=_stable_id(market, cluster.cluster_key, [a.id for a in cluster_articles]),
        market=issue_market,  # type: ignore[arg-type]
        rank=rank,
        issue_title=_pick_issue_title(cluster, articles),
        subtitle=_pick_subtitle(cluster, articles),
        direction=direction,  # type: ignore[arg-type]
        source_count=len(sources),
        article_count=len(cluster_articles),
        updated_at=updated_at,
        summary=None,
        related_symbols=related_symbols,
        related_sectors=[],
        articles=issue_articles,
        signals=signals,
    )


def _score(issue: MarketIssue) -> float:
    s = issue.signals
    return s.recency_score * 0.5 + s.source_diversity_score * 0.3 + s.mention_score * 0.2


async def build_market_issues(
    *,
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    max_rows: int = 500,
) -> MarketIssuesResponse:
    """Build a ranked list of `MarketIssue` for a given market window."""
    articles = await _load_recent_articles(
        market=market, window_hours=window_hours, max_rows=max_rows
    )
    if not articles:
        return MarketIssuesResponse(
            market=market if market in ("kr", "us", "crypto", "all") else "all",  # type: ignore[arg-type]
            as_of=now_kst_naive(),
            window_hours=window_hours,
            items=[],
        )

    clusters = _cluster_articles(articles, market=market)
    issues = [
        _to_market_issue(
            cluster=c, articles=articles, market=market, window_hours=window_hours, rank=0
        )
        for c in clusters
        if c.article_indexes
    ]
    issues.sort(key=_score, reverse=True)
    issues = issues[:limit]
    for i, issue in enumerate(issues, start=1):
        # rank is read-only on the model; build a copy with the rank set.
        issues[i - 1] = issue.model_copy(update={"rank": i})

    return MarketIssuesResponse(
        market=market if market in ("kr", "us", "crypto", "all") else "all",  # type: ignore[arg-type]
        as_of=now_kst_naive(),
        window_hours=window_hours,
        items=issues,
    )
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_news_issue_clustering.py -v`
Expected: 6 passed

- [ ] **Step 3: Commit**

```bash
git add app/services/news_issue_clustering_service.py
git commit -m "feat(news): deterministic market issue clustering MVP service (ROB-130)"
```

---

## Task 10: Read-only router for market issues

**Files:**
- Create: `app/routers/news_issues.py`
- Modify: `app/main.py`
- Test: `tests/test_router_news_issues.py`

- [ ] **Step 1: Write the failing API contract test**

```python
# tests/test_router_news_issues.py
"""Contract tests for the read-only /trading/api/news-issues endpoint (ROB-130)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.timezone import now_kst_naive
from app.main import app
from app.routers.dependencies import get_authenticated_user
from app.services import news_issue_clustering_service as clustering


def _mk(id: int, title: str, source: str, minutes_ago: int = 30, market: str = "us"):
    return SimpleNamespace(
        id=id,
        title=title,
        summary=None,
        source=source,
        feed_source=f"rss_{source}",
        url=f"https://example.com/{id}",
        market=market,
        keywords=[],
        article_published_at=now_kst_naive() - timedelta(minutes=minutes_ago),
        stock_symbol=None,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        clustering,
        "_load_recent_articles",
        AsyncMock(
            return_value=[
                _mk(1, "Amazon raises guidance on AWS demand", "cnbc"),
                _mk(2, "AWS growth boosts Amazon outlook", "bloomberg"),
            ]
        ),
    )

    async def _stub_user():
        return SimpleNamespace(id=1, email="t@example.com")

    app.dependency_overrides[get_authenticated_user] = _stub_user
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_authenticated_user, None)


@pytest.mark.unit
def test_market_issues_returns_ranked_list(client):
    resp = client.get("/trading/api/news-issues?market=us&window_hours=24&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == "us"
    assert body["window_hours"] == 24
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 1
    first = body["items"][0]
    assert first["rank"] == 1
    assert "AMZN" in [rs["symbol"] for rs in first["related_symbols"]]
    assert "signals" in first
    for key in ("recency_score", "source_diversity_score", "mention_score"):
        assert 0.0 <= first["signals"][key] <= 1.0


@pytest.mark.unit
def test_market_issues_invalid_market_rejected(client):
    resp = client.get("/trading/api/news-issues?market=eu")
    assert resp.status_code == 422
```

Run: `uv run pytest tests/test_router_news_issues.py -v`
Expected: FAIL — endpoint not registered.

- [ ] **Step 2: Implement the router**

```python
# app/routers/news_issues.py
"""Market issue clustering router (ROB-130).

Read-only. No order/watch/intent/broker imports allowed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.news_issues import MarketIssuesResponse
from app.services.news_issue_clustering_service import build_market_issues

router = APIRouter(prefix="/trading", tags=["news-issues"])


@router.get("/api/news-issues", response_model=MarketIssuesResponse)
async def get_news_issues(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: Literal["all", "kr", "us", "crypto"] = Query("all"),
    window_hours: int = Query(24, ge=1, le=168),
    limit: int = Query(20, ge=1, le=100),
) -> MarketIssuesResponse:
    return await build_market_issues(
        market=market, window_hours=window_hours, limit=limit
    )
```

- [ ] **Step 3: Register the router in `app/main.py`**

Locate the section in `app/main.py` where existing routers are registered (e.g. `app.include_router(news_radar.router)`) and add directly below it:

```python
from app.routers import news_issues  # near the other router imports
...
app.include_router(news_issues.router)  # alongside news_radar registration
```

(Use Edit to add the import next to the existing `from app.routers import news_radar` line and the include alongside `app.include_router(news_radar.router)`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_router_news_issues.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/routers/news_issues.py app/main.py tests/test_router_news_issues.py
git commit -m "feat(news): /trading/api/news-issues read-only router (ROB-130)"
```

---

## Task 11: Optional MCP tool wrapper

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py`

- [ ] **Step 1: Add `get_market_issues` tool registration**

Append a third tool to `news_handlers.py`. Keep existing `get_market_news`/`search_news` unchanged.

```python
# In NEWS_TOOL_NAMES near the top, add:
NEWS_TOOL_NAMES = ["get_market_news", "search_news", "get_market_issues"]


# At the bottom of `register_tools`, add:
@mcp.tool(
    name="get_market_issues",
    description=(
        "Read-only deterministic market issue clusters from collected news "
        "(ROB-130). Groups recent articles by entity/topic and ranks by "
        "recency + source diversity + mention count."
    ),
)
async def get_market_issues(
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
) -> dict[str, Any]:
    from app.services.news_issue_clustering_service import build_market_issues
    response = await build_market_issues(
        market=market, window_hours=window_hours, limit=limit
    )
    return response.model_dump(mode="json")
```

- [ ] **Step 2: Smoke check**

Run: `uv run python -c "from app.mcp_server.tooling.news_handlers import NEWS_TOOL_NAMES; print(NEWS_TOOL_NAMES)"`
Expected: `['get_market_news', 'search_news', 'get_market_issues']`

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py
git commit -m "feat(mcp): expose get_market_issues read-only MCP tool (ROB-130)"
```

---

## Task 12: Runbook + ROB-129 metadata handoff doc

**Files:**
- Create: `docs/runbooks/news-issue-clustering.md`

- [ ] **Step 1: Write the runbook**

```markdown
# News Issue Clustering — Runbook (ROB-130)

Read-only market issue clustering API and ticker-news fallback.

## Surface

- HTTP: `GET /trading/api/news-issues?market=all|kr|us|crypto&window_hours=1..168&limit=1..100`
- MCP tool: `get_market_issues(market, window_hours, limit)` — read-only
- Schema: `app.schemas.news_issues.MarketIssuesResponse`
- Service entry point: `app.services.news_issue_clustering_service.build_market_issues`
- Fallback entry point: `app.services.llm_news_service.get_news_articles_with_fallback`
- Entity matcher: `app.services.news_entity_matcher.match_symbols_for_article`

## Behavior

1. Loads recent `news_articles` rows (window default 24h, max 500 rows).
2. Tags each row with the deterministic alias matcher (KR/US/crypto).
3. Clusters by primary entity; remaining rows clustered by 3-gram title shingles
   with Jaccard >= 0.34.
4. Ranks clusters by `0.5*recency + 0.3*source_diversity + 0.2*mention_score`.
5. Returns stable hash-derived issue IDs; same input → same IDs.

## ROB-129 metadata consumption

Once the news-ingestor PR ships per-article `candidate_symbols` /
`candidate_sectors` JSONB metadata, replace step (2):

1. If `article.candidate_symbols` is present, prefer those over alias matching.
2. Use `match_symbols_for_article` only as a fallback when the candidate list
   is empty.

The contract additions (TODO ROB-129):
- `news_articles.candidate_symbols: JSONB | None`
- `news_articles.candidate_sectors: JSONB | None`

These are nullable, additive, and backward compatible.

## Operational checks

```bash
curl -sS "$BASE/trading/api/news-issues?market=us&window_hours=6&limit=5" \
  -H "Cookie: $AUTH" | jq '.items[0]'
```

Expected fields per item: `id`, `rank`, `issue_title`, `subtitle`, `direction`,
`source_count`, `article_count`, `signals.{recency_score,source_diversity_score,mention_score}`.

## Performance / safety boundaries

- No LLM calls.
- No broker/order/intent imports.
- No DB writes; pure read query against `news_articles`.
- `max_rows=500` caps SQL fan-out; tune via call-site if needed.

## Smoke validation after deploy (Hermes)

```bash
uv run pytest tests/test_news_entity_matcher.py \
              tests/test_news_issue_clustering.py \
              tests/test_news_stage_fallback.py \
              tests/test_router_news_issues.py -v
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/news-issue-clustering.md
git commit -m "docs(news): runbook for issue clustering and ticker fallback (ROB-130)"
```

---

## Task 13: Full validation pass

- [ ] **Step 1: Run targeted news/research test suite**

Run: `uv run pytest tests/test_news_entity_matcher.py tests/test_news_issue_clustering.py tests/test_news_stage_fallback.py tests/test_news_stage_on_demand.py tests/test_router_news_issues.py tests/test_market_news_briefing_formatter.py tests/test_router_news_radar.py -v`
Expected: all passing

- [ ] **Step 2: Run lint + format check**

Run: `make lint`
Expected: clean

- [ ] **Step 3: Run typecheck**

Run: `make typecheck`
Expected: clean (or no new errors vs main)

- [ ] **Step 4: Run full unit suite**

Run: `uv run pytest tests/ -v -m "not integration and not slow"`
Expected: green; no regressions in existing news/preopen tests

- [ ] **Step 5: If anything fails, fix and re-commit**

Use a focused commit per fix. Do not amend earlier commits.

---

## Task 14: Open the PR

- [ ] **Step 1: Push branch**

```bash
git push -u origin "$(git branch --show-current)"
```

- [ ] **Step 2: Open PR with handoff content**

Use `gh pr create` with a body covering:

- Linear: ROB-130
- Summary: 3 bullets — entity matcher / fallback / clustering
- Schema/API additions: `/trading/api/news-issues`, `get_market_issues` MCP tool, `MarketIssuesResponse` schema
- Backward compatibility: `get_news_articles`, `/trading/api/news-radar`, `get_market_news`, preopen, news_radar all unchanged on the wire
- Fallback behaviour examples for AMZN and 005930 (curl snippets from runbook)
- Tests run (commands from Task 13)
- ROB-129 dependency: optional — the matcher consumes `candidate_symbols` once the news-ingestor PR adds them; until then, deterministic alias matcher handles tagging
- DB migrations: none in this PR
- Hermes follow-up: deploy + production smoke per `docs/runbooks/news-issue-clustering.md`

---

## Self-Review Checklist (run after Task 14 push)

1. **Spec coverage** — every ROB-130 acceptance criterion maps to a task:
   - AMZN/005930 fallback returning relevant news → Task 4–6
   - Clustering MVP returning stable ranked items → Task 7–9
   - Read-only, backward-compatible endpoint/schema → Task 10
   - Existing market news briefing tests still pass → Task 13
   - Targeted + practical full validation → Task 13
   - PR description with API/schema/test info → Task 14
2. **Placeholder scan** — no TBD/TODO/“implement later” remain.
3. **Type consistency** — `match_symbols`, `match_symbols_for_article`,
   `get_news_articles_with_fallback`, `build_market_issues`, `MarketIssue`,
   `MarketIssuesResponse` names match across tasks.
