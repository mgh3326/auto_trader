# ROB-628 + ROB-629 — MCP 뉴스/랭킹 in-context 사용성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the three MCP news/ranking tools usable directly in the main context by normalizing oversized news responses, adding a one-call cross-market holdings catalyst sweep, and giving the KIS foreigners ranking honest named fields + buy/sell split + market_cap/liquidity hygiene.

**Architecture:** PR-A = ROB-628 — `get_market_news`/`get_market_issues` response-shaping (shared truncation util + `detail` param + de-duplicated briefing sections + excluded/size caps) plus the new `get_holdings_news` catalyst sweep. PR-B = ROB-629 — the KIS foreigners ranking (named `foreign_net_qty`/`foreign_net_amount`, `foreign_net_buy`/`foreign_net_sell` split, market_cap backfill, default-ON liquidity filter). Both PRs are migration-0 pure read-layer with zero file overlap, so they ship and review independently.

**Tech Stack:** Python 3.13, FastMCP MCP tools, SQLAlchemy async, pytest (uv run pytest), ruff + ty.

## Global Constraints

- 마이그레이션 0 (no alembic). 순수 read-layer; no broker/order/watch mutation.
- Runtime LLM ownership boundary: no in-process LLM provider imports (Gemini/OpenAI/etc.).
- No silent drop / no fabricate: every reduction (truncation, size-cap item drop, liquidity filter, session-stale) signals via status/degraded_reason; size-cap also sets truncated_for_size: bool.
- Canonical names: news_text.truncate_text(value, max_length); NEWS_SUMMARY_MAX_CHARS=240; NEWS_RESPONSE_MAX_CHARS=8000; detail: Literal["headline_only","summary","full"]="summary"; get_holdings_news(symbols, limit_per_symbol=5); HOLDINGS_NEWS_MAX_SYMBOLS=30; HOLDINGS_NEWS_CONCURRENCY=4; ranking_type "foreign_net_buy"/"foreign_net_sell" ("foreigners" alias); row keys foreign_net_qty/foreign_net_amount; include_illiquid:bool=False; FOREIGNERS_MIN_NET_AMOUNT_KRW default 100000000, FOREIGNERS_MIN_MARKET_CAP_KRW default 30000000000 (operator-tunable env).
- foreigners ranking is KRX-only (KR domestic).
- Gate per task: uv run ruff format/check + ty check app/ + the task's tests green.

---

## PR-A — ROB-628 (뉴스 응답 shape + 보유종목 촉매 sweep)

> Its own branch/PR. Branch from `origin/main`. Tasks 1→4. Task 1 is the foundation (shared util); Tasks 2 and 3 depend on it; Task 4 is independent within PR-A.

### Task 1: Shared truncation util (A0)

**Files:**
- Create: `app/services/news_text.py`
- Modify: `app/services/news_radar_service.py` (lines 56-70: regex constants + `_plain_text` body; call sites 140 & 143 stay unchanged)
- Test: `tests/services/test_news_text.py` (new); regression `tests/test_news_radar_service.py::test_service_returns_plain_text_snippets_for_html_summaries` (must stay green, no edit)

**Interfaces:**
- Produces (the canonical shared util consumed by Tasks 2, 3):
  - `truncate_text(value: str | None, max_length: int | None = None) -> str | None` — HTML-strip (`unescape` + remove `<[^>]+>` tags) + whitespace-collapse + single-char ellipsis "…"; returns `None` for `None`/empty-after-strip; output length `<= max_length`; `max_length=None` means strip-only (no truncation).
  - `NEWS_SUMMARY_MAX_CHARS = 240`
  - `NEWS_RESPONSE_MAX_CHARS = 8000`

**Notes:** Author `news_text.py` ONCE here — Tasks 2 (A1) and 3 (A2) only `import` it. Pure stdlib (`re` + `from html import unescape`); zero new deps — do NOT introduce BeautifulSoup/lxml. The `max_length: int | None = None` default is an intentional strict superset of the canonical `truncate_text(value, max_length)` contract: it keeps the strip-only path (radar title call at line 143) expressible while every cross-task caller still passes an int positionally. Keep `_plain_text` as a 1-line delegating wrapper so the existing radar test stays byte-identical; drop the now-unused `import re` / `from html import unescape` from `news_radar_service.py` (grep confirms they were only used by `_plain_text`). The "…" is a single U+2026 char (not three dots) — match exactly in assertions.

- [ ] **Step 1: Write the failing test** — create `tests/services/test_news_text.py`:

```python
"""Unit tests for app/services/news_text.py (ROB-628)."""

from __future__ import annotations

import pytest

from app.services.news_text import (
    NEWS_RESPONSE_MAX_CHARS,
    NEWS_SUMMARY_MAX_CHARS,
    truncate_text,
)

pytestmark = pytest.mark.unit


class TestTruncateText:
    def test_none_input_returns_none(self):
        assert truncate_text(None) is None
        assert truncate_text(None, 240) is None

    def test_blank_after_strip_returns_none(self):
        # Whitespace-only / tag-only collapses to empty -> None.
        assert truncate_text("   ") is None
        assert truncate_text("\n\t  ") is None
        assert truncate_text("<br/><p></p>") is None

    def test_short_text_unchanged_without_max_length(self):
        assert truncate_text("Hello world") == "Hello world"

    def test_short_text_unchanged_with_generous_max_length(self):
        assert truncate_text("Hello world", 240) == "Hello world"

    def test_whitespace_is_collapsed_and_stripped(self):
        assert truncate_text("  Hello   \n  world  ") == "Hello world"

    def test_html_tags_stripped_and_entities_unescaped(self):
        raw = (
            '<p><a rel="nofollow" href="https://x.test">Bitcoin Magazine</a>'
            '<br /> <img src="https://x.test/i.jpg" />'
            "Bitcoin bounces as Iran strike unsettles risk assets &amp; oil.</p>"
        )
        result = truncate_text(raw)
        assert result is not None
        assert "<" not in result and ">" not in result
        assert "&amp;" not in result
        assert "&" in result  # entity was unescaped, not dropped
        assert result == (
            "Bitcoin Magazine Bitcoin bounces as Iran strike "
            "unsettles risk assets & oil."
        )

    def test_long_text_truncated_with_ellipsis(self):
        result = truncate_text("abcdefgh", 5)
        assert result == "abcd…"
        assert len(result) == 5
        assert result.endswith("…")

    def test_exact_boundary_not_truncated(self):
        # len(text) == max_length -> returned unchanged, no ellipsis.
        result = truncate_text("abcde", 5)
        assert result == "abcde"
        assert "…" not in result

    def test_one_over_boundary_is_truncated(self):
        # len(text) == max_length + 1 -> truncated to max_length chars.
        result = truncate_text("abcdef", 5)
        assert result == "abcd…"
        assert len(result) == 5

    def test_truncation_right_strips_before_ellipsis(self):
        # Cut lands on a space -> rstrip removes it before appending ellipsis.
        result = truncate_text("ab cdef", 4)
        # cleaned text "ab cdef" len 7 > 4 -> "ab "[:3].rstrip()="ab" + "…"
        assert result == "ab…"

    def test_truncation_applies_after_html_strip(self):
        # HTML stripped first, then length measured on cleaned text.
        result = truncate_text("<b>abcdefgh</b>", 5)
        assert result == "abcd…"

    def test_summary_cap_truncates_long_korean_summary(self):
        body = "가" * (NEWS_SUMMARY_MAX_CHARS + 50)
        result = truncate_text(body, NEWS_SUMMARY_MAX_CHARS)
        assert result is not None
        assert len(result) == NEWS_SUMMARY_MAX_CHARS
        assert result.endswith("…")

    def test_non_str_value_is_coerced(self):
        # ported behaviour: str(value) coercion before stripping.
        assert truncate_text(12345) == "12345"  # type: ignore[arg-type]


class TestConstants:
    def test_summary_cap_value(self):
        assert NEWS_SUMMARY_MAX_CHARS == 240

    def test_response_cap_value(self):
        assert NEWS_RESPONSE_MAX_CHARS == 8000

    def test_response_cap_larger_than_summary_cap(self):
        assert NEWS_RESPONSE_MAX_CHARS > NEWS_SUMMARY_MAX_CHARS
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/services/test_news_text.py -v`. Expect ImportError / ModuleNotFoundError (`app.services.news_text` does not yet exist).

- [ ] **Step 3: Write the implementation** — create `app/services/news_text.py`:

```python
"""Shared news text normalization helpers (ROB-628).

HTML-strip + whitespace-collapse + ellipsis truncation, ported verbatim from
``news_radar_service._plain_text`` so every news MCP tool shares one
truncation contract instead of re-implementing it.

Read-only pure helpers. No DB writes. No broker calls. No mutation.
"""

from __future__ import annotations

import re
from html import unescape

# Per-item summary cap (e.g. each article's snippet in detail="summary").
NEWS_SUMMARY_MAX_CHARS = 240
# Whole-response soft cap used by size-capping callers (truncated_for_size).
NEWS_RESPONSE_MAX_CHARS = 8000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def truncate_text(
    value: str | None, max_length: int | None = None
) -> str | None:
    """Strip HTML, collapse whitespace, and optionally ellipsis-truncate.

    Behaviour is ported 1:1 from ``news_radar_service._plain_text``:

    - ``None`` input -> ``None``.
    - HTML entities are unescaped (``&amp;`` -> ``&``), HTML tags are replaced
      with a single space, and runs of whitespace are collapsed to one space
      then stripped.
    - If the cleaned text is empty after stripping -> ``None``.
    - When ``max_length`` is provided and the cleaned text is longer than it,
      the result is hard-capped to ``max_length`` characters total: the first
      ``max_length - 1`` characters (right-stripped) followed by a single
      ellipsis ("…"). At exactly ``max_length`` characters the text is returned
      unchanged.
    - When ``max_length`` is ``None`` no truncation is applied (strip-only).
    """
    if value is None:
        return None
    text = unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if max_length is not None and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text
```

  Then refactor `app/services/news_radar_service.py` — replace the local regex constants + `_plain_text` body (lines 56-70) with a delegating wrapper that preserves the existing `value: Any` / keyword-only `max_length` call surface used at lines 140 & 143:

```python
from app.services.news_text import truncate_text


def _plain_text(value: Any, *, max_length: int | None = None) -> str | None:
    return truncate_text(value, max_length)
```

  Move `_HTML_TAG_RE` / `_WHITESPACE_RE` into `news_text.py` and remove the now-unused local copies from `news_radar_service.py`; drop the now-unused `import re` / `from html import unescape` from `news_radar_service.py` (grep first to confirm no other reference). Output is byte-identical for both the strip-only (`max_length=None`) and truncate (`max_length=280`) paths, so the radar snapshot/assertions do not change.

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/services/test_news_text.py tests/test_news_radar_service.py::test_service_returns_plain_text_snippets_for_html_summaries -v`. Expect all PASS (new unit tests + the unchanged radar regression).

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/services/news_text.py app/services/news_radar_service.py tests/services/test_news_text.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

### Task 2: get_market_news response shaping (A1)

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py` — imports (lines 1-20), `_article_to_dict` (lines 23-48), `_briefing_sections_to_dict` (lines 51-71), `_get_market_news_impl` (lines 74-206), new `_apply_size_cap`, `get_market_news` tool registration (lines 210-242)
- Test: `tests/mcp_server/tooling/test_news_handlers.py` (new)

**Interfaces:**
- Consumes (from Task 1): `from app.services.news_text import NEWS_RESPONSE_MAX_CHARS, NEWS_SUMMARY_MAX_CHARS, truncate_text`
- Produces (payload top-level shape later context relies on): adds `excluded_total: int`, `truncated_for_size: bool` (default False), `size_truncation: dict` (present only when truncated_for_size is True: `{dropped_news, dropped_excluded, response_chars, max_chars}`); `status` values now include `"truncated_for_size"`; `excluded_news` capped to `<= limit`. `_article_to_dict(article, *, detail="summary", include_crypto_relevance=False, briefing_relevance=None)`; `_briefing_sections_to_dict(sections)` (drops `include_crypto_relevance` param; per-section shape `{section_id, title, count, article_ids, relevance}`, no `items` key); `_get_market_news_impl(..., detail: str = "summary")`; tool `get_market_news(..., detail: Literal["headline_only","summary","full"]="summary")`.

**Notes:** `news_text.py` is already authored in Task 1 — only import it here, do NOT recreate it. Behaviour change: default `detail="summary"` now HTML-strips + caps summaries (previously raw full summary); `detail="full"` restores the old raw behaviour — documented in the tool description. Existing tests verified unaffected: `tests/test_mcp_get_market_news_quality_gate.py`, `tests/test_mcp_news_crypto_relevance.py`, `tests/test_market_news_briefing_formatter.py` (~298-311) all use short/None summaries and assert `section_id`/ids/`excluded_reason` (no test asserts the OLD `briefing_sections[*]["items"]` shape — grep clean; the `["items"]` assertions in `tests/test_preopen_market_news_briefing.py` are a SEPARATE schema path, unaffected). Size cap drop order is deterministic: clear `excluded_news` fully first, then pop trailing `news[]` one at a time; `size_truncation` reports both counts (never a silent drop). `sources`/`feed_sources` are computed before the cap and intentionally left as-is (they reflect what was fetched). Mocking pattern: `patch("app.mcp_server.tooling.news_handlers.get_news_articles", new=AsyncMock(return_value=(rows, total)))` with `SimpleNamespace` rows; `format_market_news_briefing` / `rank_crypto_news_for_briefing` / `classify_title_noise` / `score_crypto_news_article` are NOT mocked (tests rely on real deterministic behaviour: 'Sponsored…'→sponsored, 'XRP Price Prediction…'→price_prediction, 'My plumber…'→personal_finance; 'Fed holds rates steady…'/'Nvidia AI chip demand…'/'Fed rate cut hopes…' → CLEAN).

- [ ] **Step 1: Write the failing test** — create `tests/mcp_server/tooling/test_news_handlers.py`:

```python
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.mcp_server.tooling.news_handlers import _get_market_news_impl
from app.services.news_text import NEWS_RESPONSE_MAX_CHARS, NEWS_SUMMARY_MAX_CHARS

_PATCH_TARGET = "app.mcp_server.tooling.news_handlers.get_news_articles"


def _row(
    article_id: int,
    title: str,
    *,
    feed_source: str = "rss_test",
    market: str = "us",
    summary: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id}",
        source="Test Source",
        feed_source=feed_source,
        market=market,
        summary=summary,
        article_published_at=datetime(2026, 6, 11, 9, 0, 0),
        keywords=[],
        stock_symbol=None,
        stock_name=None,
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_summary_truncates_to_max_chars():
    long_summary = "A" * 500  # no whitespace -> deterministic length
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary=long_summary)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10)  # detail default

    summary = result["news"][0]["summary"]
    assert summary is not None
    assert len(summary) <= NEWS_SUMMARY_MAX_CHARS == 240
    assert summary.endswith("…")
    assert summary != long_summary


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_headline_only_drops_summary():
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary="B" * 500)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(
            market="us", limit=10, detail="headline_only"
        )

    item = result["news"][0]
    assert "summary" not in item
    assert item["title"] == "Fed holds rates steady as inflation cools"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_detail_full_keeps_untruncated_summary():
    long_summary = "C" * 500
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary=long_summary)]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10, detail="full")

    summary = result["news"][0]["summary"]
    assert summary == long_summary
    assert len(summary) == 500
    assert not summary.endswith("…")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_briefing_sections_carry_only_ids_and_relevance():
    rows = [
        _row(1, "Fed rate cut hopes lift S&P 500 futures before CPI report"),
        _row(2, "Nvidia AI chip demand lifts Nasdaq semiconductor stocks"),
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 2))):
        result = await _get_market_news_impl(
            market="us", limit=10, briefing_filter=True
        )

    sections = result["briefing_sections"]
    assert sections
    news_ids = {n["id"] for n in result["news"]}
    for section in sections:
        # no full article dict re-embedded
        assert "items" not in section
        assert "summary" not in section
        assert "url" not in section
        assert set(section) == {"section_id", "title", "count", "article_ids", "relevance"}
        assert isinstance(section["article_ids"], list)
        assert all(isinstance(aid, int) for aid in section["article_ids"])
        assert isinstance(section["relevance"], list)
        assert len(section["relevance"]) == len(section["article_ids"]) == section["count"]
        # relevance dicts are scoring metadata, not article bodies
        for rel in section["relevance"]:
            assert "score" in rel
            assert "title" not in rel  # no article title leaking through
        # ids point at bodies that live in news[]
        for aid in section["article_ids"]:
            assert aid in news_ids
    # bodies are present exactly once, in news[]
    assert all("title" in n for n in result["news"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_excluded_news_capped_to_limit_with_total():
    rows = [
        _row(1, "Sponsored: The 5 best coins to buy now"),
        _row(2, "XRP Price Prediction: Could XRP reach $10 by 2027?"),
        _row(3, "My plumber charged $160 to fix a problem — do I pay again?"),
        _row(4, "Sponsored: The 5 best coins to buy now"),
        _row(5, "XRP Price Prediction: Could XRP reach $10 by 2027?"),
        _row(6, "Fed holds rates steady as inflation cools"),
        _row(7, "Nvidia AI chip demand lifts Nasdaq semiconductor stocks"),
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 7))):
        result = await _get_market_news_impl(market="us", limit=2)

    assert result["excluded_total"] == 5
    assert len(result["excluded_news"]) == 2  # capped to limit
    assert result["count"] == 2  # two clean items survive
    assert all("excluded_reason" in e for e in result["excluded_news"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_oversized_payload_truncated_for_size_stays_under_cap():
    # detail="full" keeps big summaries -> blows past the size cap.
    big = "X" * 3000
    rows = [
        _row(i, f"Fed holds rates steady as inflation cools {i}", summary=big)
        for i in range(1, 8)
    ]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 7))):
        result = await _get_market_news_impl(market="us", limit=20, detail="full")

    assert result["truncated_for_size"] is True
    assert result["degraded_reason"]
    assert result["status"] == "truncated_for_size"
    assert "size_truncation" in result
    assert result["size_truncation"]["dropped_news"] > 0
    # honest accounting: count matches what actually remains
    assert result["count"] == len(result["news"]) < 7
    # the serialized response actually fits under the hard cap
    assert len(json.dumps(result, default=str)) <= NEWS_RESPONSE_MAX_CHARS


@pytest.mark.asyncio
@pytest.mark.unit
async def test_small_payload_not_flagged_truncated():
    rows = [_row(1, "Fed holds rates steady as inflation cools", summary="short")]
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=(rows, 1))):
        result = await _get_market_news_impl(market="us", limit=10)

    assert result["truncated_for_size"] is False
    assert "size_truncation" not in result
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/mcp_server/tooling/test_news_handlers.py -v`. Expect failures (e.g. `detail` kwarg unsupported / `excluded_total` & `truncated_for_size` keys absent / briefing_sections still carry `items`).

- [ ] **Step 3: Write the implementation** — edit `app/mcp_server/tooling/news_handlers.py`.

  (3a) Imports header (lines 1-20) becomes — add `import json`, `Literal`, and the three `news_text` symbols:

```python
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from app.models.news import NewsArticle
from app.services.crypto_news_relevance_service import (
    rank_crypto_news_for_briefing,
    score_crypto_news_article,
)
from app.services.llm_news_service import get_news_articles
from app.services.market_news_briefing_formatter import (
    BriefingSection,
    format_market_news_briefing,
)
from app.services.market_news_noise import classify_title_noise, noise_reason
from app.services.news_text import (
    NEWS_RESPONSE_MAX_CHARS,
    NEWS_SUMMARY_MAX_CHARS,
    truncate_text,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues"]
```

  (3b) REGION 1 — replace `_article_to_dict` (lines 23-48):

```python
def _article_to_dict(
    article: NewsArticle,
    *,
    detail: str = "summary",
    include_crypto_relevance: bool = False,
    briefing_relevance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": article.id,
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "feed_source": article.feed_source,
        "market": article.market,
        "published_at": article.article_published_at.isoformat()
        if article.article_published_at
        else None,
        "keywords": article.keywords,
        "stock_symbol": article.stock_symbol,
        "stock_name": article.stock_name,
    }
    # detail controls the body field: headline_only omits it entirely; full keeps
    # the raw summary; summary (default) HTML-strips + caps to NEWS_SUMMARY_MAX_CHARS.
    if detail == "headline_only":
        pass
    elif detail == "full":
        item["summary"] = article.summary
    else:  # "summary" (default / unknown -> safe default)
        item["summary"] = truncate_text(article.summary, NEWS_SUMMARY_MAX_CHARS)
    if include_crypto_relevance:
        item["crypto_relevance"] = score_crypto_news_article(article).as_dict()
    if briefing_relevance is not None:
        item["briefing_relevance"] = briefing_relevance
    return item
```

  (3c) REGION 2 — replace `_briefing_sections_to_dict` (lines 51-71):

```python
def _briefing_sections_to_dict(
    sections: list[BriefingSection],
) -> list[dict[str, Any]]:
    # ROB-628: sections carry only article ids + per-article relevance. The full
    # article bodies are emitted exactly once, in news[]. This stops the response
    # from re-embedding every article dict per section.
    return [
        {
            "section_id": section.section_id,
            "title": section.title,
            "count": len(section.items),
            "article_ids": [item.article.id for item in section.items],
            "relevance": [item.relevance.as_dict() for item in section.items],
        }
        for section in sections
    ]
```

  (3d) REGION 3 — replace `_get_market_news_impl` (lines 74-206) and add `_apply_size_cap` directly after it:

```python
async def _get_market_news_impl(
    market: str | None = None,
    hours: int | None = 24,
    feed_source: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    limit: int | None = 20,
    briefing_filter: bool = False,
    detail: str = "summary",
) -> dict[str, Any]:
    hours = hours or 24
    limit = limit or 20

    query_limit = limit
    if market in {"crypto", "us", "kr"} and briefing_filter:
        # Pull a slightly larger window so ranking can hide low-signal noise
        # without returning an under-filled briefing when relevant items exist.
        query_limit = max(limit * 3, limit)

    articles, total = await get_news_articles(
        market=market,
        hours=hours,
        feed_source=feed_source,
        source=source,
        keyword=keyword,
        limit=query_limit,
    )

    # ROB-502 quality gate (always on): noise-classified titles never reach
    # the default list — they move to excluded_news with an explicit reason.
    gated_articles = []
    noise_excluded: list[dict[str, Any]] = []
    for article in articles:
        noise = classify_title_noise(article.title or "")
        if noise:
            item = _article_to_dict(article, detail=detail)
            item["excluded_reason"] = noise_reason(noise)
            noise_excluded.append(item)
        else:
            gated_articles.append(article)
    articles = gated_articles

    excluded_news: list[dict[str, Any]] = []
    briefing_summary = None
    briefing_sections: list[dict[str, Any]] = []
    if market == "crypto":
        if briefing_filter:
            ranking = rank_crypto_news_for_briefing(list(articles), limit=limit)
            news_list = [
                _article_to_dict(
                    item.article, detail=detail, include_crypto_relevance=True
                )
                for item in ranking.included
            ]
            excluded_news = [
                _article_to_dict(
                    item.article, detail=detail, include_crypto_relevance=True
                )
                for item in ranking.excluded
            ]
            briefing_summary = ranking.summary
            briefing = format_market_news_briefing(
                list(articles), market=market, limit=limit
            )
            briefing_sections = _briefing_sections_to_dict(briefing.sections)
        else:
            news_list = [
                _article_to_dict(a, detail=detail, include_crypto_relevance=True)
                for a in articles
            ]
    elif briefing_filter and market in {"us", "kr"}:
        briefing = format_market_news_briefing(
            list(articles), market=market, limit=limit
        )
        news_list = [
            _article_to_dict(
                item.article,
                detail=detail,
                briefing_relevance=item.relevance.as_dict(),
            )
            for section in briefing.sections
            for item in section.items
        ]
        excluded_news = [
            _article_to_dict(
                item.article,
                detail=detail,
                briefing_relevance=item.relevance.as_dict(),
            )
            for item in briefing.excluded
        ]
        briefing_summary = briefing.summary
        briefing_sections = _briefing_sections_to_dict(briefing.sections)
    else:
        news_list = [_article_to_dict(a, detail=detail) for a in articles]
    excluded_news = noise_excluded + excluded_news
    # ROB-628: cap excluded_news to `limit`; excluded_total keeps the true count.
    excluded_total = len(excluded_news)
    excluded_news = excluded_news[:limit]
    source_names = list({a.get("source") for a in news_list if a.get("source")})
    feed_source_names = list(
        {a.get("feed_source") for a in news_list if a.get("feed_source")}
    )

    # ROB-502: degraded states are explicit — no filler when nothing passes.
    status = "ok"
    degraded_reason = None
    if total == 0:
        status = "no_recent_articles"
        degraded_reason = (
            f"no articles in the last {hours}h window — "
            "ingestion may be stale or paused"
        )
    elif not news_list:
        status = "no_meaningful_items"
        degraded_reason = (
            f"{total} article(s) in window, but none passed the quality gate "
            f"({excluded_total} excluded — see excluded_news reasons); "
            "no filler is generated"
        )

    payload: dict[str, Any] = {
        "surface": "quality_gated_market_briefing",
        "advisory": (
            "Quality-gated broad-market DB-backed surface for briefing only; "
            "NOT investment-decision evidence. Use get_news for one symbol's "
            "catalysts, or get_holdings_news to sweep your holdings' catalysts "
            "in one call (ROB-628). Noise-classified items appear in "
            "excluded_news with reasons instead of the main list."
        ),
        "market": market,
        "status": status,
        "degraded_reason": degraded_reason,
        "count": len(news_list),
        "total": total,
        "news": news_list,
        "sources": sorted(source_names),
        "feed_sources": sorted(feed_source_names),
        "briefing_filter": bool(briefing_filter),
        "briefing_summary": briefing_summary,
        "briefing_sections": briefing_sections,
        "excluded_news": excluded_news,
        "excluded_total": excluded_total,
        "truncated_for_size": False,
    }

    return _apply_size_cap(payload)


def _apply_size_cap(payload: dict[str, Any]) -> dict[str, Any]:
    # ROB-628: hard size cap. If the serialized response exceeds
    # NEWS_RESPONSE_MAX_CHARS, drop excluded_news first (least decision-critical),
    # then trailing news[] items, until under the cap. Never silently: set
    # truncated_for_size, append to degraded_reason, and report counts.
    if len(json.dumps(payload, default=str)) <= NEWS_RESPONSE_MAX_CHARS:
        return payload

    dropped_excluded = len(payload["excluded_news"])
    payload["excluded_news"] = []

    dropped_news = 0
    while (
        len(json.dumps(payload, default=str)) > NEWS_RESPONSE_MAX_CHARS
        and payload["news"]
    ):
        payload["news"].pop()
        dropped_news += 1

    payload["count"] = len(payload["news"])
    payload["truncated_for_size"] = True
    payload["size_truncation"] = {
        "dropped_news": dropped_news,
        "dropped_excluded": dropped_excluded,
        "response_chars": len(json.dumps(payload, default=str)),
        "max_chars": NEWS_RESPONSE_MAX_CHARS,
    }
    reason = (
        f"response exceeded {NEWS_RESPONSE_MAX_CHARS} chars — dropped "
        f"{dropped_excluded} excluded and {dropped_news} trailing news item(s) "
        "to fit (use detail='headline_only' or a smaller limit for the full set)"
    )
    payload["degraded_reason"] = (
        f"{payload['degraded_reason']}; {reason}"
        if payload.get("degraded_reason")
        else reason
    )
    if payload["status"] == "ok":
        payload["status"] = "truncated_for_size"
    return payload
```

  (3e) REGION 4 — replace the `get_market_news` tool registration (lines 210-242):

```python
    @mcp.tool(
        name="get_market_news",
        description=(
            "[Quality-gated broad market briefing surface; NOT investment-decision "
            "evidence — use get_news for symbol-level decisions] "
            "Get recent market news with a noise gate always on (ROB-502): "
            "personal-finance/lifestyle/sponsored/price-prediction/broad-tech items "
            "move to excluded_news with an excluded_reason instead of the main list. "
            "status is 'ok' | 'no_meaningful_items' | 'no_recent_articles' | "
            "'truncated_for_size' with degraded_reason — no filler is generated. "
            "detail controls per-article body: 'headline_only' (no summary), "
            "'summary' (default, HTML-stripped + capped to 240 chars), or 'full' "
            "(raw untruncated). briefing_sections carry only article_ids + relevance; "
            "bodies live once in news[]. excluded_news is capped to limit "
            "(excluded_total = true count). Oversized responses set truncated_for_size "
            "and drop trailing items rather than overflow. Supports filtering by "
            "market, publisher (source), collection path (feed_source), and keyword. "
            "briefing_filter=True additionally formats market-specific sections for "
            "kr/us and ranks crypto-relevant items."
        ),
    )
    async def get_market_news(
        market: str | None = None,
        hours: int = 24,
        feed_source: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        briefing_filter: bool = False,
        detail: Literal["headline_only", "summary", "full"] = "summary",
    ) -> dict[str, Any]:
        return await _get_market_news_impl(
            market=market,
            hours=hours,
            feed_source=feed_source,
            source=source,
            keyword=keyword,
            limit=limit,
            briefing_filter=briefing_filter,
            detail=detail,
        )
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/mcp_server/tooling/test_news_handlers.py tests/test_mcp_get_market_news_quality_gate.py tests/test_mcp_news_crypto_relevance.py tests/test_market_news_briefing_formatter.py -v`. Expect all PASS (new shaping tests + the unaffected existing suites).

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/mcp_server/tooling/news_handlers.py tests/mcp_server/tooling/test_news_handlers.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

### Task 3: get_market_issues response shaping (A2)

**Files:**
- Modify: `app/schemas/news_issues.py` — `MarketIssuesResponse` (lines 78-87): add `truncated_for_size: bool = False`
- Modify: `app/services/news_issue_clustering_service.py` — imports; new `_article_summary_for_detail`; `_to_market_issue` (lines 279-333) add `detail` kw-only + use helper for summary; `build_market_issues` (lines 423-504) add `detail` kw-only + thread into `_to_market_issue`
- Modify: `app/mcp_server/tooling/news_handlers.py` — new `_get_market_issues_impl` + `_enforce_market_issues_size_cap`; `get_market_issues` tool registration (lines 244-267) add `detail` + delegate
- Modify: `app/routers/news_issues.py` (line 31) — pass `detail="full"` to preserve the web contract
- Test: extend `tests/test_news_issue_clustering.py`; new `tests/test_mcp_get_market_issues_size_cap.py`

**Interfaces:**
- Consumes (from Task 1): `from app.services.news_text import NEWS_SUMMARY_MAX_CHARS, truncate_text` (clustering service) and `from app.services.news_text import NEWS_RESPONSE_MAX_CHARS` (news_handlers — already imported by Task 2). Consumes (from Task 2): `import json`, `Literal` already present in `news_handlers.py`.
- Produces: `build_market_issues(*, market="all", window_hours=24, limit=20, max_rows=500, detail: Literal["headline_only","summary","full"]="summary")`; `_to_market_issue(..., detail=...)`; `_article_summary_for_detail(raw_summary, detail) -> str | None`; `_get_market_issues_impl(market="all", window_hours=24, limit=20, detail=...)`; `_enforce_market_issues_size_cap(payload) -> dict`; `MarketIssuesResponse.truncated_for_size: bool`. Per-detail `MarketIssueArticle.summary`: headline_only→None, summary→`truncate_text(raw, NEWS_SUMMARY_MAX_CHARS)`, full→raw verbatim.

**Notes:** `news_text.py` is authored ONCE in Task 1 — A2's draft shows the module body for self-runnability only; do NOT re-author it here, just import. `news_handlers.py` already gained `import json` + `Literal` in Task 2, so Task 3 adds no new news_handlers import beyond what's there (it reuses the `NEWS_RESPONSE_MAX_CHARS` import from Task 2). `MarketIssueArticle.summary` is already `str | None = None` — no schema change needed for headline_only/truncation; the ONLY schema edit is `truncated_for_size: bool = False` on `MarketIssuesResponse` (both models use `ConfigDict(extra="forbid")`, so the field MUST be declared or model_dump rejects it). Size cap runs in the handler AFTER `model_dump` on the plain dict (three deterministic passes: collapse trailing issues to a single anchor article → drop whole trailing issues keeping ≥1 → last-resort collapse survivors), uses `json.dumps(..., ensure_ascii=False)` so Korean text isn't length-inflated; never returns `[]`, never fabricates; `status` stays the closed Literal value (trim signal carried by `truncated_for_size` + appended counted `degraded_reason`). Back-compat: `app/routers/news_issues.py:31` gets explicit `detail="full"` to keep the web's untruncated member summaries; `feed_news_service.py:447` needs NO change (only reads `issues_resp.items` for id linking). Existing tests in `test_news_issue_clustering.py` / `test_mcp_get_market_news_quality_gate.py` / `test_market_news_quality_gate_rob502.py` use short/None summaries → default truncation yields None/unchanged, stay green. New async tests carry `@pytest.mark.unit` + `@pytest.mark.asyncio` per repo convention. The Amazon pair (id=1/id=2, two distinct sources) is the proven entity-clustering fixture (article_count==2).

- [ ] **Step 1: Write the failing test** — (1) append to `tests/test_news_issue_clustering.py` (reuse the existing `_mk` fixture + the `clustering._load_recent_articles` `AsyncMock` monkeypatch convention):

```python
# (add near the top, after the existing imports)
# from app.services.news_text import NEWS_SUMMARY_MAX_CHARS   # imported lazily inside tests below

_LONG_SUMMARY = (
    "Amazon Web Services reported accelerating demand across cloud, AI, and "
    "advertising segments, with management raising full-year guidance and "
    "pointing to a record multi-year backlog that underpins the outlook. "
) * 4  # comfortably exceeds NEWS_SUMMARY_MAX_CHARS (240)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detail_summary_truncates_member_article_summary(monkeypatch):
    from app.services.news_text import NEWS_SUMMARY_MAX_CHARS

    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand",
            source="cnbc", summary=_LONG_SUMMARY),
        _mk(id=2, title="AWS growth boosts Amazon outlook",
            source="bloomberg", summary=_LONG_SUMMARY),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(
        market="us", window_hours=24, limit=10, detail="summary"
    )
    summaries = [a.summary for iss in result.items for a in iss.articles]
    assert summaries, "expected at least one clustered member article"
    for s in summaries:
        assert s is not None
        assert len(s) <= NEWS_SUMMARY_MAX_CHARS
        assert s.endswith("…")
    assert result.truncated_for_size is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detail_headline_only_drops_member_summary(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand",
            source="cnbc", summary=_LONG_SUMMARY),
        _mk(id=2, title="AWS growth boosts Amazon outlook",
            source="bloomberg", summary=_LONG_SUMMARY),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(
        market="us", window_hours=24, limit=10, detail="headline_only"
    )
    summaries = [a.summary for iss in result.items for a in iss.articles]
    assert summaries
    assert all(s is None for s in summaries)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detail_full_keeps_member_summary_verbatim(monkeypatch):
    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand",
            source="cnbc", summary=_LONG_SUMMARY),
        _mk(id=2, title="AWS growth boosts Amazon outlook",
            source="bloomberg", summary=_LONG_SUMMARY),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(
        market="us", window_hours=24, limit=10, detail="full"
    )
    summaries = [a.summary for iss in result.items for a in iss.articles]
    assert summaries
    assert all(s == _LONG_SUMMARY for s in summaries)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_detail_defaults_to_summary_truncation(monkeypatch):
    from app.services.news_text import NEWS_SUMMARY_MAX_CHARS

    rows = [
        _mk(id=1, title="Amazon raises guidance on AWS demand",
            source="cnbc", summary=_LONG_SUMMARY),
        _mk(id=2, title="AWS growth boosts Amazon outlook",
            source="bloomberg", summary=_LONG_SUMMARY),
    ]
    monkeypatch.setattr(
        clustering, "_load_recent_articles", AsyncMock(return_value=rows)
    )

    result = await clustering.build_market_issues(
        market="us", window_hours=24, limit=10
    )  # no detail kwarg -> default "summary"
    summaries = [a.summary for iss in result.items for a in iss.articles]
    assert summaries
    assert all(
        s is not None and len(s) <= NEWS_SUMMARY_MAX_CHARS for s in summaries
    )
```

  (2) Create `tests/test_mcp_get_market_issues_size_cap.py`:

```python
"""ROB-628 AREA A2: get_market_issues hard response-size cap.

Oversized responses are trimmed (trailing issues / member articles dropped)
and explicitly flagged via truncated_for_size + degraded_reason — never a
silent drop, never fabricated filler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.timezone import now_kst_naive
from app.schemas.news_issues import (
    IssueQualityGate,
    IssueSignals,
    MarketIssue,
    MarketIssueArticle,
    MarketIssuesResponse,
)
from app.services.news_text import NEWS_RESPONSE_MAX_CHARS


def _big_article(article_id: int) -> MarketIssueArticle:
    return MarketIssueArticle(
        id=article_id,
        title=f"Issue article {article_id} headline that is reasonably descriptive",
        url=f"https://example.com/{article_id}",
        source="cnbc",
        feed_source="rss_cnbc",
        published_at=now_kst_naive(),
        summary="x" * 1500,  # large full-detail body to blow past the cap
        matched_terms=["alpha", "beta"],
    )


def _big_issue(rank: int) -> MarketIssue:
    return MarketIssue(
        id=f"{rank:016d}",
        market="us",
        rank=rank,
        issue_title=f"Issue {rank}",
        subtitle="subtitle",
        direction="neutral",
        source_count=2,
        article_count=3,
        updated_at=now_kst_naive(),
        summary=None,
        related_symbols=[],
        related_sectors=[],
        articles=[_big_article(rank * 100 + k) for k in range(3)],
        signals=IssueSignals(
            recency_score=0.5, source_diversity_score=0.5, mention_score=0.5
        ),
    )


def _response(n_issues: int) -> MarketIssuesResponse:
    return MarketIssuesResponse(
        market="us",
        as_of=now_kst_naive(),
        window_hours=24,
        items=[_big_issue(r) for r in range(1, n_issues + 1)],
        status="ok",
        degraded_reason=None,
        quality_gate=IssueQualityGate(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oversized_response_trims_issues_and_flags():
    from app.mcp_server.tooling import news_handlers

    big = _response(n_issues=12)
    raw_len = len(json.dumps(big.model_dump(mode="json"), ensure_ascii=False))
    assert raw_len > NEWS_RESPONSE_MAX_CHARS  # precondition: trimmer must engage

    with patch(
        "app.services.news_issue_clustering_service.build_market_issues",
        new=AsyncMock(return_value=big),
    ):
        result = await news_handlers._get_market_issues_impl(
            market="us", window_hours=24, limit=20, detail="full"
        )

    assert result["truncated_for_size"] is True
    assert result["degraded_reason"]
    assert "size cap" in result["degraded_reason"]
    # Hard cap honoured.
    assert len(json.dumps(result, ensure_ascii=False)) <= NEWS_RESPONSE_MAX_CHARS
    # Issues were genuinely trimmed (no fabrication), but never emptied.
    assert 1 <= len(result["items"]) < 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_within_cap_response_untouched():
    from app.mcp_server.tooling import news_handlers

    small = _response(n_issues=1)
    assert (
        len(json.dumps(small.model_dump(mode="json"), ensure_ascii=False))
        <= NEWS_RESPONSE_MAX_CHARS
    )

    with patch(
        "app.services.news_issue_clustering_service.build_market_issues",
        new=AsyncMock(return_value=small),
    ):
        result = await news_handlers._get_market_issues_impl(market="us")

    assert result["truncated_for_size"] is False
    assert result["degraded_reason"] is None
    assert len(result["items"]) == 1
    assert len(result["items"][0]["articles"]) == 3  # member articles intact


@pytest.mark.unit
def test_size_cap_helper_is_deterministic_and_counts():
    from app.mcp_server.tooling.news_handlers import _enforce_market_issues_size_cap

    payload = _response(n_issues=12).model_dump(mode="json")
    original_issues = len(payload["items"])
    capped = _enforce_market_issues_size_cap(payload)

    assert capped["truncated_for_size"] is True
    assert len(json.dumps(capped, ensure_ascii=False)) <= NEWS_RESPONSE_MAX_CHARS
    assert len(capped["items"]) < original_issues
    # Reason names both the dropped-issue and dropped-article counts.
    assert "issue(s)" in capped["degraded_reason"]
    assert "member article(s)" in capped["degraded_reason"]


@pytest.mark.unit
def test_size_cap_preserves_existing_degraded_reason():
    from app.mcp_server.tooling.news_handlers import _enforce_market_issues_size_cap

    payload = _response(n_issues=12).model_dump(mode="json")
    payload["degraded_reason"] = "preexisting note"
    capped = _enforce_market_issues_size_cap(payload)
    assert capped["degraded_reason"].startswith("preexisting note; ")
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_news_issue_clustering.py tests/test_mcp_get_market_issues_size_cap.py -v`. Expect failures (`detail` kwarg unsupported / `truncated_for_size` absent / `_get_market_issues_impl` & `_enforce_market_issues_size_cap` not defined).

- [ ] **Step 3: Write the implementation**.

  (3a) `app/schemas/news_issues.py` — add `truncated_for_size` to `MarketIssuesResponse`:

```python
class MarketIssuesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketIssueMarket | Literal["all"]
    as_of: datetime
    window_hours: int
    items: list[MarketIssue] = Field(default_factory=list)
    status: IssuesStatus = "ok"
    degraded_reason: str | None = None
    quality_gate: IssueQualityGate | None = None
    truncated_for_size: bool = False
```

  (3b) `app/services/news_issue_clustering_service.py` — add to the imports block (top of file):

```python
from typing import Literal
# ... existing imports ...
from app.services.news_text import NEWS_SUMMARY_MAX_CHARS, truncate_text
```

  Add the NEW helper just above `_to_market_issue`:

```python
def _article_summary_for_detail(
    raw_summary: str | None,
    detail: Literal["headline_only", "summary", "full"],
) -> str | None:
    """Shape a member-article summary by requested verbosity (ROB-628).

    headline_only -> drop; summary -> HTML-strip + <=NEWS_SUMMARY_MAX_CHARS;
    full -> verbatim (preserves the pre-ROB-628 contract).
    """
    if detail == "headline_only":
        return None
    if detail == "full":
        return raw_summary
    return truncate_text(raw_summary, NEWS_SUMMARY_MAX_CHARS)
```

  Replace `_to_market_issue` (add `detail` kw-only param + use the helper for `summary`):

```python
def _to_market_issue(
    *,
    cluster: _Cluster,
    articles: list[NewsArticle],
    market: str,
    window_hours: int,
    rank: int,
    detail: Literal["headline_only", "summary", "full"] = "summary",
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
            mention_count=sum(
                1
                for a in cluster_articles
                if m.matched_term.lower()
                in f"{a.title or ''} {getattr(a, 'summary', '') or ''}".lower()
            ),
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
            summary=_article_summary_for_detail(getattr(a, "summary", None), detail),
            matched_terms=[
                m.matched_term
                for m in match_symbols_for_article(
                    title=a.title,
                    summary=getattr(a, "summary", None),
                    keywords=getattr(a, "keywords", None) or [],
                    market=market if market != "all" else None,
                )
            ],
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
```

  Replace `build_market_issues` (add `detail` kw-only param + thread it into `_to_market_issue`):

```python
async def build_market_issues(
    *,
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    max_rows: int = 500,
    detail: Literal["headline_only", "summary", "full"] = "summary",
) -> MarketIssuesResponse:
    """Build a ranked list of `MarketIssue` for a given market window.

    ROB-502: the output is meaningfulness-gated. ROB-628: `detail` controls
    member-article summary verbosity (headline_only/summary/full); default
    "summary" truncates each member summary to NEWS_SUMMARY_MAX_CHARS to keep
    MCP responses within the token budget.
    """
    response_market = market if market in ("kr", "us", "crypto", "all") else "all"
    loaded = await _load_recent_articles(
        market=market, window_hours=window_hours, max_rows=max_rows
    )
    if not loaded:
        return MarketIssuesResponse(
            market=response_market,  # type: ignore[arg-type]
            as_of=now_kst_naive(),
            window_hours=window_hours,
            items=[],
            status="no_recent_articles",
            degraded_reason=(
                f"no articles in the last {window_hours}h window — "
                "ingestion may be stale or paused"
            ),
            quality_gate=IssueQualityGate(),
        )

    articles = [a for a in loaded if not classify_title_noise(a.title or "")]
    noise_excluded = len(loaded) - len(articles)

    clusters = _cluster_articles(articles, market=market)
    clusters, merged_count = _merge_near_duplicate_shingle_clusters(clusters, articles)
    issues = [
        _to_market_issue(
            cluster=c,
            articles=articles,
            market=market,
            window_hours=window_hours,
            rank=0,
            detail=detail,
        )
        for c in clusters
        if c.article_indexes
    ]
    meaningful = [issue for issue in issues if _is_meaningful(issue)]
    excluded_thin = len(issues) - len(meaningful)

    meaningful.sort(key=_score, reverse=True)
    meaningful = meaningful[:limit]
    for i, issue in enumerate(meaningful, start=1):
        meaningful[i - 1] = issue.model_copy(update={"rank": i})

    gate = IssueQualityGate(
        articles_total=len(loaded),
        noise_articles_excluded=noise_excluded,
        clusters_total=len(issues),
        clusters_merged=merged_count,
        clusters_excluded_thin=excluded_thin,
    )
    status = "ok"
    degraded_reason = None
    if not meaningful:
        status = "no_meaningful_items"
        degraded_reason = (
            f"{len(loaded)} article(s) in window, but none formed a meaningful "
            f"cluster (noise_excluded={noise_excluded}, "
            f"thin_clusters={excluded_thin}) — no filler is generated"
        )

    return MarketIssuesResponse(
        market=response_market,  # type: ignore[arg-type]
        as_of=now_kst_naive(),
        window_hours=window_hours,
        items=meaningful,
        status=status,  # type: ignore[arg-type]
        degraded_reason=degraded_reason,
        quality_gate=gate,
    )
```

  (3c) `app/mcp_server/tooling/news_handlers.py` — add the NEW module-level impl + size-cap helper (place after `_apply_size_cap` from Task 2). The `import json`, `Literal`, and `NEWS_RESPONSE_MAX_CHARS` import already exist from Task 2:

```python
async def _get_market_issues_impl(
    market: str = "all",
    window_hours: int = 24,
    limit: int = 20,
    detail: Literal["headline_only", "summary", "full"] = "summary",
) -> dict[str, Any]:
    from app.services.news_issue_clustering_service import build_market_issues

    response = await build_market_issues(
        market=market, window_hours=window_hours, limit=limit, detail=detail
    )
    payload = response.model_dump(mode="json")
    return _enforce_market_issues_size_cap(payload)


def _enforce_market_issues_size_cap(payload: dict[str, Any]) -> dict[str, Any]:
    """Hard cap the serialized response at NEWS_RESPONSE_MAX_CHARS (ROB-628).

    Trims in three deterministic passes — (1) collapse trailing issues to a
    single anchor article, (2) drop whole trailing issues (always keep >=1),
    (3) collapse the survivors' articles as a last resort — then flips
    truncated_for_size and appends a counted degraded_reason. Never silently
    drops or fabricates: every removal is reflected in the flag + reason.
    """

    def _encoded_len() -> int:
        return len(json.dumps(payload, ensure_ascii=False))

    if _encoded_len() <= NEWS_RESPONSE_MAX_CHARS:
        return payload

    items = payload.get("items") or []
    original_issue_count = len(items)
    original_article_count = sum(len(it.get("articles") or []) for it in items)

    # Pass 1: trim trailing member articles down to a single anchor article.
    for item in reversed(items):
        if _encoded_len() <= NEWS_RESPONSE_MAX_CHARS:
            break
        arts = item.get("articles") or []
        if len(arts) > 1:
            item["articles"] = arts[:1]

    # Pass 2: drop whole trailing issues (keep at least one) until under cap.
    while len(items) > 1 and _encoded_len() > NEWS_RESPONSE_MAX_CHARS:
        items.pop()

    # Pass 3: last resort — collapse any remaining multi-article survivors.
    if _encoded_len() > NEWS_RESPONSE_MAX_CHARS:
        for item in items:
            arts = item.get("articles") or []
            if len(arts) > 1:
                item["articles"] = arts[:1]

    payload["items"] = items
    kept_issue_count = len(items)
    kept_article_count = sum(len(it.get("articles") or []) for it in items)
    dropped_issues = original_issue_count - kept_issue_count
    dropped_articles = original_article_count - kept_article_count

    payload["truncated_for_size"] = True
    reason = (
        f"response exceeded the {NEWS_RESPONSE_MAX_CHARS}-char size cap; trimmed "
        f"{dropped_issues} issue(s) and {dropped_articles} member article(s) to "
        "fit — re-query with a narrower market/window or detail='headline_only' "
        "for the full set"
    )
    existing = payload.get("degraded_reason")
    payload["degraded_reason"] = f"{existing}; {reason}" if existing else reason
    return payload
```

  Replace the `get_market_issues` tool registration (lines 244-267) — add `detail` + delegate to the impl, update description:

```python
    @mcp.tool(
        name="get_market_issues",
        description=(
            "Read-only deterministic market issue clusters from collected news "
            "(ROB-130, quality-gated per ROB-502). Groups recent articles by "
            "entity/topic, merges near-duplicate syndicated stories, and ranks by "
            "recency + source diversity + mention count. Noise-classified articles "
            "never enter clustering, and thin clusters (single article AND single "
            "source, non-official feed) are withheld. status/degraded_reason/"
            "quality_gate report what the gate did; empty results are explicit "
            "(no_meaningful_items), never filler. detail controls member-article "
            "summary verbosity: 'headline_only' drops summaries, 'summary' "
            "(default) truncates each to 240 chars, 'full' keeps them verbatim. "
            "The response is hard-capped at 8000 chars; if exceeded, trailing "
            "issues/articles are trimmed and truncated_for_size + degraded_reason "
            "are set (never a silent drop)."
        ),
    )
    async def get_market_issues(
        market: str = "all",
        window_hours: int = 24,
        limit: int = 20,
        detail: Literal["headline_only", "summary", "full"] = "summary",
    ) -> dict[str, Any]:
        return await _get_market_issues_impl(
            market=market, window_hours=window_hours, limit=limit, detail=detail
        )
```

  (3d) `app/routers/news_issues.py` (line 31) — preserve the web contract (full member summaries):

```python
    return await build_market_issues(
        market=market, window_hours=window_hours, limit=p.limit, detail="full"
    )
```

  `feed_news_service.py:447` needs NO change — it only reads `issues_resp.items` for id-based linking and never surfaces member-article summaries.

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/test_news_issue_clustering.py tests/test_mcp_get_market_issues_size_cap.py tests/test_market_news_quality_gate_rob502.py -v`. Expect all PASS.

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/schemas/news_issues.py app/services/news_issue_clustering_service.py app/mcp_server/tooling/news_handlers.py app/routers/news_issues.py tests/test_news_issue_clustering.py tests/test_mcp_get_market_issues_size_cap.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

### Task 4: get_holdings_news sweep tool (A3)

**Files:**
- Modify: `app/mcp_server/tooling/fundamentals/_news.py` — add `import asyncio`, constants, two helpers, the impl (existing `handle_get_news` unchanged)
- Modify: `app/mcp_server/tooling/fundamentals_handlers.py` — extend the `_news` import (line 42), add `"get_holdings_news"` to `FUNDAMENTALS_TOOL_NAMES` (after line 80), register the tool inside `_register_fundamentals_tools_impl(mcp)` (after line 144)
- Test: `tests/mcp_server/tooling/test_get_holdings_news.py` (new); optionally extend any exhaustive `FUNDAMENTALS_TOOL_NAMES` enumeration in `tests/test_mcp_fundamentals_tools.py`

**Interfaces:**
- Consumes (reused, unchanged): `symbol_news_service.fetch_symbol_news(symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0) -> SymbolNewsFetchResult`; `portfolio_holdings._collect_portfolio_positions(*, account, market, include_current_price, account_name=None, user_id=_MCP_USER_ID, is_mock=False) -> tuple[list[dict], list[dict], str|None, str|None]` (lazy-imported); `shared.normalize_symbol_input`, `shared.is_korean_equity_code`, `shared.is_crypto_market`, `_helpers.normalize_market_with_crypto`; module-level `_INSTRUMENT_BY_MARKET`.
- Produces: `HOLDINGS_NEWS_MAX_SYMBOLS=30`, `HOLDINGS_NEWS_CONCURRENCY=4`; `_lean_holdings_news_item(article)`; `_infer_holdings_news_market(symbol)`; `_resolve_holdings_news_candidates(symbols) -> tuple[list[dict], str|None]`; `_get_holdings_news_impl(symbols=None, limit_per_symbol=5)`; tool `get_holdings_news(symbols: list[str] | None = None, limit_per_symbol: int = 5)`. Envelope: `{symbols_requested, symbols_resolved, count, results:[{symbol, name, market, status, news:[{title,url,source,published_at,relevance}], degraded_reason?}], degraded_reason?}`.

**Notes:** Independent within PR-A (does NOT depend on Task 1's `news_text`; leanness here is field-projection, not text truncation). `_collect_portfolio_positions` is imported LAZILY inside `_resolve_holdings_news_candidates` (not at module top) for import-cycle safety (portfolio_holdings pulls in broker clients) and to keep it monkeypatchable on the `portfolio_holdings` module. Call it with `include_current_price=False` (sweep only needs symbols, skips the expensive price fan-out). Fail-soft layers (no silent drop / no fabricate): per-row try/except turns an unexpected raise into `status="error"` + `degraded_reason=<ExcName>`; holdings-resolution failure returns `[]` + top-level `degraded_reason="holdings_resolution_failed: ..."`; the 30-cap adds a top-level `degraded_reason` and `symbols_resolved` is an explicit prefix of `symbols_requested`; junk positions (blank/unknown market) dropped via `market not in _INSTRUMENT_BY_MARKET`. `name` is None for passed-through `symbols=[...]` (honest, no enrichment lookup); populated from holdings position `name` in holdings mode. Registration is always-on (registry.py:125 calls `register_fundamentals_tools` unconditionally), matching `get_news`. The registration test uses `tests._mcp_tooling_support.build_tools()`. Before the full suite, grep `tests/test_mcp_fundamentals_tools.py` for any exhaustive `FUNDAMENTALS_TOOL_NAMES` enumeration that needs `"get_holdings_news"` added.

- [ ] **Step 1: Write the failing test** — create `tests/mcp_server/tooling/test_get_holdings_news.py`:

```python
# tests/mcp_server/tooling/test_get_holdings_news.py
"""get_holdings_news cross-market sweep (ROB-628 P2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.mcp_server.tooling import portfolio_holdings
from app.mcp_server.tooling.fundamentals import _news
from app.services import symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _article(symbol: str, market: str, idx: int) -> SymbolNewsArticle:
    url = f"https://news/{symbol}/{idx}"
    return SymbolNewsArticle(
        provider="naver" if market == "kr" else "finnhub",
        market=market,
        symbol=symbol,
        external_article_id=f"{symbol}-{idx}",
        title=f"{symbol} headline {idx}",
        source_name="한국경제" if market == "kr" else "Reuters",
        canonical_url=url,
        summary=None,
        published_at=datetime(2026, 6, 20, 9, idx, tzinfo=UTC),
        fetched_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        provider_metadata={
            "source_item": {"title": f"{symbol} headline {idx}", "url": url},
            "relevance": {"status": "pending"},
        },
    )


def _ok_result(symbol: str, market: str, n: int = 1) -> SymbolNewsFetchResult:
    provider = "naver" if market == "kr" else "finnhub"
    arts = [_article(symbol, market, i) for i in range(n)]
    return SymbolNewsFetchResult(
        symbol, market, provider, "ok", 5, n, arts, excluded_count=0
    )


def _patch_fetch(monkeypatch, fn) -> None:
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", fn)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_symbols_passed_through(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, int]] = []

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        calls.append((symbol, market, instrument_type, limit))
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["005930", "AAPL", "KRW-BTC"], limit_per_symbol=5
    )

    # passed through (normalized) and not re-resolved from holdings
    assert out["symbols_requested"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["symbols_resolved"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["count"] == 3
    assert "degraded_reason" not in out
    # market inferred per symbol + correct instrument_type + limit threaded
    assert {(c[1], c[2]) for c in calls} == {
        ("kr", "equity_kr"),
        ("us", "equity_us"),
        ("crypto", "crypto"),
    }
    assert all(c[3] == 5 for c in calls)
    # name unknown for passed-through symbols
    assert all(row["name"] is None for row in out["results"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_row_shape_is_lean(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market, n=2)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=["AAPL"], limit_per_symbol=5)

    row = out["results"][0]
    assert set(row.keys()) == {"symbol", "name", "market", "status", "news"}
    assert row["symbol"] == "AAPL"
    assert row["market"] == "us"
    assert row["status"] == "ok"
    assert len(row["news"]) == 2
    item = row["news"][0]
    assert set(item.keys()) == {
        "title",
        "url",
        "source",
        "published_at",
        "relevance",
    }
    assert item["title"] == "AAPL headline 0"
    assert item["url"] == "https://news/AAPL/0"
    assert item["source"] == "Reuters"
    assert item["published_at"].startswith("2026-06-20T09:00")
    assert item["relevance"] == {"status": "pending"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_symbols_resolves_holdings(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return (
            [
                {"symbol": "005930", "market": "kr", "name": "삼성전자"},
                {"symbol": "AAPL", "market": "us", "name": "Apple"},
                {"symbol": "KRW-BTC", "market": "crypto", "name": "비트코인"},
                # duplicate (same symbol+market, different account) -> de-duped
                {"symbol": "005930", "market": "kr", "name": "삼성전자"},
                # junk market -> dropped
                {"symbol": "XXX", "market": "", "name": "junk"},
            ],
            [],
            None,
            None,
        )

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    # holdings resolver invoked with the cheap (no-price) read
    assert captured["account"] is None
    assert captured["market"] is None
    assert captured["include_current_price"] is False
    # de-duped + junk dropped, names carried through from holdings
    assert out["symbols_resolved"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["count"] == 3
    names = {r["symbol"]: r["name"] for r in out["results"]}
    assert names == {"005930": "삼성전자", "AAPL": "Apple", "KRW-BTC": "비트코인"}
    assert "degraded_reason" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caps_symbols_at_max_with_degraded(monkeypatch) -> None:
    big = [f"{i:06d}" for i in range(35)]  # 35 distinct KR 6-digit codes

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=big, limit_per_symbol=5)

    assert len(out["symbols_requested"]) == 35
    assert len(out["symbols_resolved"]) == _news.HOLDINGS_NEWS_MAX_SYMBOLS
    assert out["count"] == _news.HOLDINGS_NEWS_MAX_SYMBOLS
    assert "degraded_reason" in out
    assert str(_news.HOLDINGS_NEWS_MAX_SYMBOLS) in out["degraded_reason"]
    # nothing fabricated/dropped silently: resolved is a prefix of requested
    assert out["symbols_resolved"] == out["symbols_requested"][
        : _news.HOLDINGS_NEWS_MAX_SYMBOLS
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_symbol_failure_is_fail_soft(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        if symbol == "AAPL":
            raise RuntimeError("boom")
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["005930", "AAPL", "TSLA"], limit_per_symbol=5
    )

    assert out["count"] == 3  # one bad symbol did not kill the sweep
    by_symbol = {r["symbol"]: r for r in out["results"]}
    assert by_symbol["AAPL"]["status"] == "error"
    assert by_symbol["AAPL"]["degraded_reason"] == "RuntimeError"
    assert by_symbol["AAPL"]["news"] == []
    # neighbours unaffected
    assert by_symbol["005930"]["news"]
    assert by_symbol["TSLA"]["news"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_row_surfaces_result_degraded(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return SymbolNewsFetchResult(
            symbol,
            market,
            "finnhub",
            "ok",
            5,
            1,
            [_article(symbol, market, 0)],
            degraded=True,
            fetch_error="TimeoutError",
        )

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=["AAPL"], limit_per_symbol=5)

    row = out["results"][0]
    assert row["status"] == "ok"
    assert row["degraded_reason"] == "TimeoutError"
    assert row["news"]  # cached articles still surfaced


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_resolution_failure_is_fail_soft(monkeypatch) -> None:
    async def boom(**kwargs):
        raise RuntimeError("kis down")

    monkeypatch.setattr(portfolio_holdings, "_collect_portfolio_positions", boom)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    assert out["count"] == 0
    assert out["results"] == []
    assert out["symbols_resolved"] == []
    assert "holdings_resolution_failed" in out["degraded_reason"]


@pytest.mark.unit
def test_get_holdings_news_registered_on_default_profile() -> None:
    from tests._mcp_tooling_support import build_tools

    tools = build_tools()
    assert "get_holdings_news" in tools
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/mcp_server/tooling/test_get_holdings_news.py -v`. Expect AttributeError (`_news._get_holdings_news_impl` / `HOLDINGS_NEWS_MAX_SYMBOLS` not defined) and the registration assertion failing.

- [ ] **Step 3: Write the implementation**.

  (3a) `app/mcp_server/tooling/fundamentals/_news.py` — add `import asyncio` immediately under `from __future__ import annotations` (line 4), so the import block top becomes:

```python
from __future__ import annotations

import asyncio

from typing import Any
```

  (All other existing imports stay; `normalize_market_with_crypto`, `_is_crypto_market`, `_is_korean_equity_code`, `_normalize_symbol_input`, `symbol_news_service`, and `_INSTRUMENT_BY_MARKET` are already present and reused.) Append the following to the END of `app/mcp_server/tooling/fundamentals/_news.py`:

```python
# ---------------------------------------------------------------------------
# get_holdings_news — cross-market catalyst-headline sweep (ROB-628 P2)
# ---------------------------------------------------------------------------

# Bound the basket so a large portfolio can't explode the fan-out, and bound
# concurrency so the per-symbol fetches don't stall the MCP event loop.
HOLDINGS_NEWS_MAX_SYMBOLS = 30
HOLDINGS_NEWS_CONCURRENCY = 4


def _lean_holdings_news_item(
    article: symbol_news_service.SymbolNewsArticle,
) -> dict[str, Any]:
    """Project a normalized article down to the lean sweep shape."""
    published_at = article.published_at
    return {
        "title": article.title,
        "url": article.canonical_url,
        "source": article.source_name,
        "published_at": published_at.isoformat() if published_at else None,
        "relevance": article.provider_metadata.get("relevance"),
    }


def _infer_holdings_news_market(symbol: str) -> str:
    """Infer 'kr'|'us'|'crypto' for a passed-through symbol (mirrors get_news)."""
    if _is_korean_equity_code(symbol):
        return "kr"
    if _is_crypto_market(symbol):
        return "crypto"
    return "us"


async def _resolve_holdings_news_candidates(
    symbols: list[str] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve the (symbol, market, name) candidates to sweep.

    Explicit ``symbols`` are normalized and passed through (market inferred per
    symbol, name unknown -> None). When ``symbols`` is omitted, current
    cross-market holdings are resolved through the canonical aggregation entry
    point ``_collect_portfolio_positions`` (KIS KR/US, Upbit, manual, Toss).
    De-dupes on (symbol, market) preserving first occurrence. Never raises.
    Returns ``(candidates, degraded_reason)``.
    """
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []

    if symbols is not None:
        for raw in symbols:
            symbol = _normalize_symbol_input(raw, None)
            if not symbol:
                continue
            market = normalize_market_with_crypto(_infer_holdings_news_market(symbol))
            key = (symbol, market)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"symbol": symbol, "market": market, "name": None})
        return candidates, None

    # Holdings mode. Lazy import keeps the portfolio_holdings dependency (and its
    # broker clients) out of the plain fundamentals import path and avoids any
    # import cycle — mirrors _collect_portfolio_positions' own lazy imports.
    from app.mcp_server.tooling.portfolio_holdings import _collect_portfolio_positions

    try:
        positions, errors, _, _ = await _collect_portfolio_positions(
            account=None,
            market=None,
            include_current_price=False,
        )
    except Exception as exc:  # noqa: BLE001 — sweep must stay fail-soft
        return [], f"holdings_resolution_failed: {type(exc).__name__}"

    degraded_reason = (
        f"holdings resolution partial ({len(errors)} source error(s))"
        if errors
        else None
    )

    for position in positions:
        symbol = str(position.get("symbol") or "").strip()
        market = str(position.get("market") or "").strip().lower()
        if not symbol or market not in _INSTRUMENT_BY_MARKET:
            continue
        key = (symbol, market)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {"symbol": symbol, "market": market, "name": position.get("name")}
        )

    return candidates, degraded_reason


async def _get_holdings_news_impl(
    symbols: list[str] | None = None,
    limit_per_symbol: int = 5,
) -> dict[str, Any]:
    """Sweep recent catalyst headlines for a basket of symbols in one call.

    ``symbols`` passed -> swept as-is (cross-market, market inferred per symbol);
    omitted -> current cross-market holdings are resolved and swept. Capped at
    HOLDINGS_NEWS_MAX_SYMBOLS; per-symbol fetch failures are isolated to that
    row (never abort the sweep). Lean rows mirror get_news but trimmed.
    """
    capped_limit = min(max(int(limit_per_symbol), 1), 50)

    candidates, resolution_degraded = await _resolve_holdings_news_candidates(symbols)

    symbols_requested = [entry["symbol"] for entry in candidates]
    degraded_reasons: list[str] = []
    if resolution_degraded:
        degraded_reasons.append(resolution_degraded)

    if len(candidates) > HOLDINGS_NEWS_MAX_SYMBOLS:
        degraded_reasons.append(
            f"resolved {len(candidates)} symbols; capped to "
            f"{HOLDINGS_NEWS_MAX_SYMBOLS} — pass a narrower symbols list to widen"
        )
        candidates = candidates[:HOLDINGS_NEWS_MAX_SYMBOLS]

    symbols_resolved = [entry["symbol"] for entry in candidates]

    semaphore = asyncio.Semaphore(HOLDINGS_NEWS_CONCURRENCY)

    async def _fetch_one(entry: dict[str, Any]) -> dict[str, Any]:
        symbol = entry["symbol"]
        market = entry["market"]
        instrument_type = _INSTRUMENT_BY_MARKET.get(market, "equity_us")
        row: dict[str, Any] = {
            "symbol": symbol,
            "name": entry["name"],
            "market": market,
            "status": "error",
            "news": [],
        }
        async with semaphore:
            try:
                result = await symbol_news_service.fetch_symbol_news(
                    symbol, market, instrument_type, limit=capped_limit
                )
            except Exception as exc:  # noqa: BLE001 — one bad symbol mustn't kill the sweep
                row["degraded_reason"] = type(exc).__name__
                return row
        row["status"] = result.status
        row["news"] = [_lean_holdings_news_item(a) for a in result.articles]
        if result.status in ("error", "unavailable"):
            row["degraded_reason"] = result.error_code or "news_unavailable"
        elif result.degraded:
            row["degraded_reason"] = result.fetch_error or "degraded"
        return row

    results = await asyncio.gather(*[_fetch_one(entry) for entry in candidates])

    payload: dict[str, Any] = {
        "symbols_requested": symbols_requested,
        "symbols_resolved": symbols_resolved,
        "count": len(results),
        "results": list(results),
    }
    if degraded_reasons:
        payload["degraded_reason"] = "; ".join(degraded_reasons)
    return payload
```

  (3b) `app/mcp_server/tooling/fundamentals_handlers.py` — extend the `_news` import (line 42):

```python
from app.mcp_server.tooling.fundamentals._news import (
    _get_holdings_news_impl,
    handle_get_news,
)
```

  Add `"get_holdings_news"` to `FUNDAMENTALS_TOOL_NAMES` (insert right after the `"get_news"`, line 80 entry):

```python
FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_news",
    "get_holdings_news",
    "get_company_profile",
    ...
}
```

  Register the tool inside `_register_fundamentals_tools_impl(mcp)`, immediately after the `get_news` block (after line 144):

```python
    @mcp.tool(
        name="get_holdings_news",
        description=(
            "Sweep recent catalyst headlines for a basket of symbols in ONE "
            "call. Pass symbols=[...] (cross-market: KR 6-digit codes, US "
            "tickers, KRW-/USDT- crypto) or OMIT symbols to sweep your CURRENT "
            "holdings across all accounts (KIS/Toss/manual/Upbit). Each symbol "
            "returns up to limit_per_symbol lean items "
            "{title,url,source,published_at,relevance}. Symbols are capped at "
            "30 (top-level degraded_reason notes the cap). A per-symbol fetch "
            "failure is isolated to that row (status + degraded_reason) and "
            "never aborts the sweep. Use get_news for one symbol's full envelope."
        ),
    )
    async def get_holdings_news(
        symbols: list[str] | None = None,
        limit_per_symbol: int = 5,
    ) -> dict[str, Any]:
        return await _get_holdings_news_impl(
            symbols=symbols,
            limit_per_symbol=limit_per_symbol,
        )
```

  Also clarify the existing `get_news` tool description (`app/mcp_server/tooling/fundamentals_handlers.py:134-137`) so single-symbol users discover the sweep — replace:

```python
        description=(
            "Get recent news for a stock or cryptocurrency. Supports US stocks "
            "(Finnhub), Korean stocks (Naver Finance), and crypto (Finnhub)."
        ),
```

  with:

```python
        description=(
            "Get recent catalyst news for ONE stock or cryptocurrency "
            "(per-symbol headlines + relevance). Supports US stocks (Finnhub), "
            "Korean stocks (Naver Finance), and crypto (Finnhub). For many "
            "symbols / your current holdings in one call, use get_holdings_news."
        ),
```

  (This + the `get_market_news` advisory edit in Task 2 close the spec §A2 cross-references; `get_holdings_news`'s own description already points back to `get_news`.)

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/mcp_server/tooling/test_get_holdings_news.py tests/test_mcp_fundamentals_tools.py -v`. Expect all PASS (sweep behaviour + registration membership).

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/mcp_server/tooling/fundamentals/_news.py app/mcp_server/tooling/fundamentals_handlers.py tests/mcp_server/tooling/test_get_holdings_news.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

## PR-B — ROB-629 (외인 랭킹)

> **Separate branch/PR.** Branch from `origin/main` — PR-B does NOT depend on PR-A (zero file overlap). Tasks 5→6 (Task 6 is logically after Task 5; they are decoupled via the `foreign_net_amount`/`trade_amount` fallback, but land in order). PR-B carries an operator 장중 검증 게이트 before merge (see end of doc).

### Task 5: foreigners named fields + buy/sell split (B1)

**Files:**
- Modify: `app/services/brokers/kis/domestic_market_data.py` (lines 168-184) — `foreign_buying_rank` add `rank_sort` param
- Modify: `app/services/brokers/kis/client.py` (lines 142-145) — `foreign_buying_rank` wrapper add `rank_sort`
- Modify: `app/mcp_server/tooling/analysis_screening.py` — new `_map_kr_foreign_row` (after line 137) + add to `__all__`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` — new `_FOREIGN_RANKING_TYPES` constant; full replacement of `get_top_stocks_impl` (lines 80-230)
- Modify: `app/mcp_server/tooling/analysis_registration.py` (lines 83-89) — `get_top_stocks` description
- Test: replace `tests/test_kis_rankings.py::test_foreign_buying_rank_api_params` (lines 188-242); update `tests/test_mcp_top_stocks.py` foreigners tests (lines 220-245, 327-350, 907-949) + add split-dispatch test

**Interfaces:**
- Consumes: existing `analysis_screening._first_present`, `_to_optional_int`, `_to_optional_float`, `_normalize_change_rate_equity`, `_map_kr_row`; `kr_market_data_state()`, `DATA_STATE_FRESH`.
- Produces (later/B2 relies on): `foreign_buying_rank(self, market="J", limit=30, rank_sort="0")` (service + client wrapper) — `rank_sort` maps to `FID_RANK_SORT_CLS_CODE`: `"0"`=순매수 상위 (net buy), `"1"`=순매도 상위 (net sell), normalized `"1" if str(rank_sort)=="1" else "0"`; `FID_ETC_CLS_CODE="1"` unchanged. `_map_kr_foreign_row(row, rank) -> dict` with named keys `foreign_net_qty` (`frgn_ntby_qty`) + `foreign_net_amount` (`frgn_ntby_tr_pbmn`); `volume`/`trade_amount`/`market_cap` honest (`acml_vol`/`acml_tr_pbmn`/`hts_avls`/`stck_avls` only, no frgn fallback). `_FOREIGN_RANKING_TYPES = frozenset({"foreign_net_buy","foreign_net_sell"})`; `get_top_stocks_impl` adds `("kr","foreign_net_buy")`/`("kr","foreign_net_sell")` to `supported_combinations` (keeps `("kr","foreigners")`); response echoes the ORIGINAL `ranking_type` ("foreigners" stays "foreigners").

**Notes:** OUT OF SCOPE for B1: the `include_illiquid` filter param (belongs to Task 6) — `foreign_buying_rank`'s new signature deliberately stops at `rank_sort`; `get_top_stocks_impl` signature is UNCHANGED here. `_map_kr_row` is intentionally left UNCHANGED (keeps its `frgn_ntby_qty`/`frgn_ntby_tr_pbmn` fallbacks — now dead for foreigners, harmless for the others). Back-compat note: any consumer that read `volume`/`trade_amount` off a `ranking_type="foreigners"` row was getting MISLABELED foreign net flow; after this change those slots are honest (None unless KIS returns acml_*), and the real signal moves to `foreign_net_qty`/`foreign_net_amount` — intentional correctness change; flag in review (grep `ranking_type.*foreigners` / `foreign_net` consumers, likely none today). **Existing test assertions to change:** `tests/test_kis_rankings.py:238` (hardcoded `FID_RANK_SORT_CLS_CODE == "0"`) → parametrized via `expected_code`; keep `FID_ETC_CLS_CODE == "1"` (line 239) unchanged. `tests/test_mcp_top_stocks.py:943-944 & 948-949` (assert `volume==frgn_ntby_qty`, `trade_amount==frgn_ntby_tr_pbmn`) → assert `foreign_net_qty`/`foreign_net_amount` with `volume is None`/`trade_amount is None`; **remove the `hts_avls` fabrication** (lines 920/929) so `market_cap` is honestly null. **Mock-signature gotcha:** the new dispatch calls `kis.foreign_buying_rank(..., rank_sort=rank_sort)` — every `MockKISClient` with `async def foreign_buying_rank(self, market, limit)` (no `rank_sort`) raises `TypeError`; the two existing tests (`test_kr_foreigners_ranking_fallback_to_mksc_shrn_iscd` 220-245, `test_kr_foreigners_routing` 327-350) MUST add `rank_sort="0"` (handled below). **Session-guard determinism:** the new guard now applies to foreign rankings and calls the REAL `kr_market_data_state()` (wall-clock dependent); the routing/named-fields/split tests monkeypatch `analysis_tool_handlers.kr_market_data_state -> "fresh"` AND carry real `frgn_ntby_qty` so `has_real_flow=True`. `DATA_STATE_FRESH == "fresh"`.

- [ ] **Step 1: Write the failing test**.

  CHANGE 1 — `tests/test_kis_rankings.py`: REPLACE the entire `test_foreign_buying_rank_api_params` method (lines 188-242, inside `class TestKISRankingAPIParams`) with the parametrized version:

```python
    @pytest.mark.parametrize(
        "call_kwargs, expected_code",
        [
            ({}, "0"),  # default = 순매수 상위 (net buy)
            ({"rank_sort": "0"}, "0"),  # explicit net buy
            ({"rank_sort": "1"}, "1"),  # 순매도 상위 (net sell)
        ],
    )
    async def test_foreign_buying_rank_api_params(
        self, monkeypatch, call_kwargs, expected_code
    ):
        """foreign_buying_rank가 rank_sort에 따라 FID_RANK_SORT_CLS_CODE를
        '0'(순매수)/'1'(순매도)로 토글하는지 검증 (ROB-629)."""
        captured_requests = []

        async def mock_get(self, url, headers, params, timeout):
            captured_requests.append({"url": url, "headers": headers, "params": params})
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "rt_cd": "0",
                "msg_cd": "",
                "msg1": "",
                "output": [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ],
            }
            return mock_response

        async def mock_get_token():
            return "test_token"

        monkeypatch.setattr("httpx.AsyncClient.get", mock_get)
        monkeypatch.setattr("app.core.config.settings.kis_access_token", "test_token")
        monkeypatch.setattr(
            "app.services.redis_token_manager.redis_token_manager.get_token",
            mock_get_token,
        )

        result = await KISClient().foreign_buying_rank(
            market="J", limit=5, **call_kwargs
        )

        assert len(captured_requests) == 1
        req = captured_requests[0]

        assert (
            req["url"]
            == f"https://openapi.koreainvestment.com:9443{FOREIGN_BUYING_RANK_URL}"
        )
        assert req["headers"]["tr_id"] == FOREIGN_BUYING_RANK_TR
        assert req["headers"]["authorization"] == "Bearer test_token"
        assert req["params"]["FID_COND_MRKT_DIV_CODE"] == "V"
        assert req["params"]["FID_COND_SCR_DIV_CODE"] == "16449"
        assert req["params"]["FID_INPUT_ISCD"] == "0000"
        assert req["params"]["FID_DIV_CLS_CODE"] == "0"
        # ROB-629: parametrized rank_sort (was hardcoded "0").
        assert req["params"]["FID_RANK_SORT_CLS_CODE"] == expected_code
        assert req["params"]["FID_ETC_CLS_CODE"] == "1"

        assert len(result) == 1
        assert result[0]["stck_shrn_iscd"] == "005930"
```

  (`test_token_retry_multiple_methods` at lines 530-592 calls `client.foreign_buying_rank(market="J", limit=5)` with no `rank_sort` — still valid against the new default. No change needed there.)

  CHANGE 2 — `tests/test_mcp_top_stocks.py`: REPLACE `test_kr_foreigners_ranking_fallback_to_mksc_shrn_iscd` (lines 220-245):

```python
    async def test_kr_foreigners_ranking_fallback_to_mksc_shrn_iscd(self, monkeypatch):
        """foreigners 랭킹에서 mksc_shrn_iscd fallback 테스트"""
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "mksc_shrn_iscd": "900210",
                        "hts_kor_isnm": "KODEX 200",
                        "stck_prpr": "35000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "20000000",
                        "frgn_ntby_tr_pbmn": "700000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 1
        assert result["rankings"][0]["symbol"] == "900210"
        assert result["rankings"][0]["name"] == "KODEX 200"
        assert result["source"] == "kis"
```

  REPLACE `test_kr_foreigners_routing` (lines 327-350):

```python
    async def test_kr_foreigners_routing(self, monkeypatch):
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "10000000",
                        "frgn_ntby_tr_pbmn": "800000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 1
```

  CHANGE 3 — `tests/test_mcp_top_stocks.py`: REPLACE `test_kr_foreigners_ranking_foreign_specific_fields` (lines 907-949) with the de-fabricated named-field test + add the split-dispatch test right after it (both inside `class TestMCPTopStocks`):

```python
    async def test_kr_foreigners_ranking_foreign_specific_fields(self, monkeypatch):
        """ROB-629: foreigners ranking surfaces foreign net flow as NAMED fields
        (foreign_net_qty / foreign_net_amount) and no longer stuffs them into the
        generic volume / trade_amount slots. hts_avls is NOT fabricated — the real
        KIS foreign ranking does not return it, so market_cap is honestly null."""
        tools = build_tools()

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    },
                    {
                        "stck_shrn_iscd": "005380",
                        "hts_kor_isnm": "LG전자",
                        "stck_prpr": "120000",
                        "prdy_ctrt": "1.5",
                        "frgn_ntby_qty": "3000000",
                        "frgn_ntby_tr_pbmn": "360000000000",
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")

        assert result["ranking_type"] == "foreigners"
        assert len(result["rankings"]) == 2

        first = result["rankings"][0]
        assert first["symbol"] == "005930"
        assert first["name"] == "삼성전자"
        # Named foreign fields — the whole point of ROB-629.
        assert first["foreign_net_qty"] == 5000000
        assert first["foreign_net_amount"] == pytest.approx(400000000000.0)
        # Generic slots are NO LONGER stuffed with the foreign values.
        assert first["volume"] is None
        assert first["trade_amount"] is None
        # market_cap honestly null (hts_avls not returned by the foreign ranking).
        assert first["market_cap"] is None

        second = result["rankings"][1]
        assert second["symbol"] == "005380"
        assert second["name"] == "LG전자"
        assert second["foreign_net_qty"] == 3000000
        assert second["foreign_net_amount"] == pytest.approx(360000000000.0)
        assert second["volume"] is None
        assert second["trade_amount"] is None

    async def test_kr_foreign_net_buy_and_sell_split_dispatch(self, monkeypatch):
        """ROB-629: foreign_net_buy passes FID rank_sort '0' (net buy),
        foreign_net_sell passes '1' (net sell); 'foreigners' aliases
        foreign_net_buy. Response echoes the caller's original ranking_type."""
        tools = build_tools()

        captured: list[str] = []

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                captured.append(rank_sort)
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        monkeypatch.setattr(
            analysis_tool_handlers, "kr_market_data_state", lambda *a, **k: "fresh"
        )

        buy = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_buy"
        )
        assert buy["ranking_type"] == "foreign_net_buy"
        assert len(buy["rankings"]) == 1

        sell = await tools["get_top_stocks"](
            market="kr", ranking_type="foreign_net_sell"
        )
        assert sell["ranking_type"] == "foreign_net_sell"
        assert len(sell["rankings"]) == 1

        alias = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert alias["ranking_type"] == "foreigners"
        assert len(alias["rankings"]) == 1

        # net buy -> "0", net sell -> "1", foreigners alias -> "0".
        assert captured == ["0", "1", "0"]
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_kis_rankings.py tests/test_mcp_top_stocks.py -v -k "foreign"`. Expect failures (`rank_sort` kwarg unsupported / `foreign_net_qty` key absent / split ranking types unsupported).

- [ ] **Step 3: Write the implementation**.

  FILE 1 — `app/services/brokers/kis/domestic_market_data.py` — replace `foreign_buying_rank` (lines 168-184):

```python
    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30, rank_sort: str = "0"
    ) -> list[dict]:
        # FID_RANK_SORT_CLS_CODE: "0"=순매수 상위(net buy), "1"=순매도 상위(net sell).
        # FID_ETC_CLS_CODE="1" pins the ranking to 외국인 (foreigners).
        rank_sort_cls_code = "1" if str(rank_sort) == "1" else "0"
        js = await self._request_with_token_retry(
            tr_id=constants.FOREIGN_BUYING_RANK_TR,
            url=self._kis_url(constants.FOREIGN_BUYING_RANK_URL),
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": rank_sort_cls_code,
                "FID_ETC_CLS_CODE": "1",
            },
            api_name="foreign_buying_rank",
        )
        return js["output"][:limit]
```

  FILE 2 — `app/services/brokers/kis/client.py` — replace the wrapper (lines 142-145):

```python
    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30, rank_sort: str = "0"
    ) -> list[dict[str, Any]]:
        return await self._market_data.foreign_buying_rank(market, limit, rank_sort)
```

  FILE 3a — `app/mcp_server/tooling/analysis_screening.py` — insert the new mapper after `_map_kr_row` (after line 137):

```python
def _map_kr_foreign_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
    """Foreigners-ranking row mapper (ROB-629).

    The foreign net-buy / net-sell ranking carries *foreign-investor net flow*,
    not whole-market accumulated volume/value. Surface that as NAMED keys
    (``foreign_net_qty`` from ``frgn_ntby_qty`` and ``foreign_net_amount`` from
    ``frgn_ntby_tr_pbmn``) instead of stuffing them into the generic ``volume``
    / ``trade_amount`` slots — the old ``_map_kr_row`` fallback silently
    mislabeled foreign net flow as total market volume.

    ``volume`` / ``trade_amount`` / ``market_cap`` stay honest: populated only
    from ``acml_vol`` / ``acml_tr_pbmn`` / ``hts_avls`` when KIS returns them
    (the foreign ranking typically omits them), else ``None`` — never
    fabricated from the foreign fields.
    """
    symbol = _first_present(row, "stck_shrn_iscd", "mksc_shrn_iscd") or ""
    name = row.get("hts_kor_isnm", "")
    price = _to_optional_float(row.get("stck_prpr"))
    change_rate = _normalize_change_rate_equity(row.get("prdy_ctrt"))
    market_cap = _to_optional_float(_first_present(row, "hts_avls", "stck_avls"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": _to_optional_int(row.get("acml_vol")),
        "market_cap": market_cap,
        "trade_amount": _to_optional_float(row.get("acml_tr_pbmn")),
        "foreign_net_qty": _to_optional_int(row.get("frgn_ntby_qty")),
        "foreign_net_amount": _to_optional_float(row.get("frgn_ntby_tr_pbmn")),
    }
```

  FILE 3b — `app/mcp_server/tooling/analysis_screening.py` — add `"_map_kr_foreign_row"` to `__all__`:

```python
__all__ = [
    "_error_payload",
    "_map_kr_row",
    "_map_kr_foreign_row",
    "_map_us_row",
    "_map_crypto_row",
    "_get_us_rankings",
    "_get_crypto_rankings",
    "_calculate_pearson_correlation",
    "_get_quote_impl",
    "_analyze_stock_impl",
    "_recommend_stocks_impl",
    "_normalize_screen_market",
    "_normalize_asset_type",
    "_normalize_sort_by",
    "_normalize_sort_order",
    "_validate_screen_filters",
    "normalize_screen_request",
    "build_screen_response",
    "screen_stocks_unified",
]
```

  FILE 4a — `app/mcp_server/tooling/analysis_tool_handlers.py` — add the module constant (after line 36 `logger = ...`):

```python
# ROB-629: the legacy "foreigners" ranking is split into directional foreign
# net-flow rankings. "foreigners" is kept as a back-compat alias for
# "foreign_net_buy".
_FOREIGN_RANKING_TYPES = frozenset({"foreign_net_buy", "foreign_net_sell"})
```

  FILE 4b — `app/mcp_server/tooling/analysis_tool_handlers.py` — full replacement of `get_top_stocks_impl` (lines 80-230):

```python
async def get_top_stocks_impl(
    market: str = "kr",
    ranking_type: str = "volume",
    limit: int = 20,
) -> dict[str, Any]:
    market = (market or "").strip().lower()
    ranking_type = (ranking_type or "").strip().lower()
    limit_clamped = max(1, min(limit, 50))

    # ROB-629: "foreigners" is the back-compat alias for the net-buy ranking.
    # Resolve the alias for dispatch + guard logic, but echo the caller's
    # ORIGINAL ranking_type in the response so existing callers keep seeing
    # "foreigners".
    resolved_ranking_type = (
        "foreign_net_buy" if ranking_type == "foreigners" else ranking_type
    )

    supported_combinations = {
        ("kr", "volume"),
        ("kr", "market_cap"),
        ("kr", "gainers"),
        ("kr", "losers"),
        ("kr", "foreigners"),
        ("kr", "foreign_net_buy"),
        ("kr", "foreign_net_sell"),
        ("us", "volume"),
        ("us", "market_cap"),
        ("us", "gainers"),
        ("us", "losers"),
        ("crypto", "volume"),
        ("crypto", "gainers"),
        ("crypto", "losers"),
        ("crypto", "relative_strength"),
    }

    key = (market, ranking_type)
    if key not in supported_combinations:
        return analysis_screening._error_payload(
            source="validation",
            message=f"Unsupported combination: market={market}, ranking_type={ranking_type}",
            query=f"market={market}, ranking_type={ranking_type}",
        )

    fetch_limit = limit_clamped
    rankings: list[dict[str, Any]] = []
    source = {"kr": "kis", "us": "yfinance", "crypto": "upbit"}.get(
        market,
        "",
    )

    try:
        if market == "kr":
            kis = KISClient()

            if ranking_type == "volume":
                data = await kis.volume_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type == "market_cap":
                data = await kis.market_cap_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type in ("gainers", "losers"):
                direction = "up" if ranking_type == "gainers" else "down"
                data = await kis.fluctuation_rank(
                    market="J", direction=direction, limit=fetch_limit
                )
                source = "kis"
            elif resolved_ranking_type in _FOREIGN_RANKING_TYPES:
                rank_sort = (
                    "1" if resolved_ranking_type == "foreign_net_sell" else "0"
                )
                data = await kis.foreign_buying_rank(
                    market="J", limit=fetch_limit, rank_sort=rank_sort
                )
                source = "kis"
            else:
                data = []

            filtered_rank = 1
            for row in data[:fetch_limit]:
                if ranking_type == "losers":
                    change_rate = analysis_screening._to_float(row.get("prdy_ctrt"))
                    if change_rate is None or change_rate >= 0:
                        continue

                if resolved_ranking_type in _FOREIGN_RANKING_TYPES:
                    mapped = analysis_screening._map_kr_foreign_row(
                        row, filtered_rank
                    )
                else:
                    mapped = analysis_screening._map_kr_row(row, filtered_rank)
                rankings.append(mapped)
                filtered_rank += 1
                if len(rankings) >= limit_clamped:
                    break

        elif market == "us":
            rankings, source = await analysis_screening._get_us_rankings(
                ranking_type, limit_clamped
            )

        elif market == "crypto":
            rankings, source = await analysis_screening._get_crypto_rankings(
                ranking_type, limit_clamped
            )

        else:
            return analysis_screening._error_payload(
                source="validation",
                message=f"Unsupported market: {market}",
                query=f"market={market}",
            )

    except Exception as exc:
        return analysis_screening._error_payload(
            source=source,
            message=str(exc),
        )

    kst_tz = datetime.timezone(datetime.timedelta(hours=9))

    # ROB-464 / ROB-629: outside the KRX regular session the directional KR
    # rankings come back as fake-0 가집계 garbage — gainers/losers with every
    # change rate at 0, and the foreign net-flow ranking with no real net flow.
    # Suppress that and tag the session instead of presenting it as live data.
    data_state: str | None = None
    if market == "kr":
        data_state = kr_market_data_state()
        if ranking_type in ("gainers", "losers"):
            has_real_move = any(r.get("change_rate") for r in rankings)
            if data_state != DATA_STATE_FRESH and not has_real_move:
                return {
                    "rankings": [],
                    "total_count": 0,
                    "market": market,
                    "ranking_type": ranking_type,
                    "timestamp": datetime.datetime.now(kst_tz).isoformat(),
                    "source": source,
                    "data_state": data_state,
                    "note": (
                        "KRX is not in regular session; gainers/losers come back "
                        "with all change rates at 0 (not a real ranking). Returning "
                        "no rows instead of fake-0 entries — retry during market "
                        "hours (09:00–15:30 KST)."
                    ),
                }
        elif resolved_ranking_type in _FOREIGN_RANKING_TYPES:
            has_real_flow = any(
                r.get("foreign_net_qty") or r.get("foreign_net_amount")
                for r in rankings
            )
            if data_state != DATA_STATE_FRESH and not has_real_flow:
                return {
                    "rankings": [],
                    "total_count": 0,
                    "market": market,
                    "ranking_type": ranking_type,
                    "timestamp": datetime.datetime.now(kst_tz).isoformat(),
                    "source": source,
                    "data_state": data_state,
                    "note": (
                        "KRX is not in regular session; the foreign net-trade "
                        "ranking comes back with no real net flow (가집계 fake-0). "
                        "Returning no rows instead of fake-0 entries — retry "
                        "during market hours (09:00–15:30 KST)."
                    ),
                }

    if len(rankings) == 0 and market == "kr" and ranking_type == "losers":
        return analysis_screening._error_payload(
            source="kis",
            message=(
                "No losing stocks found. "
                "Market may be entirely bullish or KIS API limitation."
            ),
            query="market=kr, ranking_type=losers",
            suggestion=(
                "This could indicate no stocks are declining, "
                "or the KIS API may have limited data for this ranking type."
            ),
        )

    response: dict[str, Any] = {
        "rankings": rankings,
        "total_count": len(rankings),
        "market": market,
        "ranking_type": ranking_type,
        "timestamp": datetime.datetime.now(kst_tz).isoformat(),
        "source": source,
    }
    if data_state is not None:
        response["data_state"] = data_state
    return response
```

  FILE 5 — `app/mcp_server/tooling/analysis_registration.py` — update `get_top_stocks` description (lines 83-89):

```python
    @mcp.tool(
        name="get_top_stocks",
        description=(
            "Get top stocks by ranking type across different markets (KR/US/Crypto). "
            "KR: volume, market_cap, gainers, losers, foreign_net_buy, "
            "foreign_net_sell (foreigners = back-compat alias for foreign_net_buy). "
            "Foreign rankings expose named foreign_net_qty / foreign_net_amount "
            "fields (no longer stuffed into volume/trade_amount). "
            "US: volume, market_cap, gainers, losers "
            "Crypto: volume, gainers, losers, relative_strength (vs BTC 24h)."
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/test_kis_rankings.py tests/test_mcp_top_stocks.py -v`. Expect all PASS.

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/services/brokers/kis/domestic_market_data.py app/services/brokers/kis/client.py app/mcp_server/tooling/analysis_screening.py app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_registration.py tests/test_kis_rankings.py tests/test_mcp_top_stocks.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

### Task 6: market_cap backfill + liquidity filter (B2)

**Files:**
- Modify: `app/services/invest_kr_fundamentals_snapshots/repository.py` — add `market_cap_by_symbols` (after `latest_partition()`, after line 82)
- Create: `app/mcp_server/tooling/foreigners_liquidity.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py` — `get_top_stocks_impl` add `include_illiquid` param + backfill/filter/degraded block + response metadata + import
- Modify: `app/mcp_server/tooling/analysis_registration.py` (lines 91-100) — `get_top_stocks` wrapper add `include_illiquid` (+ description note)
- Test: `tests/test_foreigners_liquidity.py` (new); add `TestForeignersLiquidity` to `tests/test_mcp_top_stocks.py`

**Interfaces:**
- Consumes (from Task 5): `foreign_buying_rank(..., rank_sort=...)` dispatch (so Task 6 mocks need `rank_sort="0"` in their `foreign_buying_rank` signatures); `_map_kr_foreign_row` row keys `foreign_net_amount`/`market_cap`/`price`; `supported_combinations` already has `("kr","foreign_net_buy")`/`("kr","foreign_net_sell")` from Task 5. Reads `foreign_net_amount` with fallback to `trade_amount` (same `frgn_ntby_tr_pbmn` KRW value), so the filter works whether or not B1 landed; `is_foreigners_ranking` matches all three spellings.
- Consumes (existing, unchanged): `InvestKrFundamentalsSnapshotsRepository.__init__(self, session)`; `KRSymbolUniverse.symbol`/`shares_outstanding`; `AsyncSessionLocal`; `shared.to_optional_float`.
- Produces: `InvestKrFundamentalsSnapshotsRepository.market_cap_by_symbols(symbols) -> dict[str, Decimal]`; module `foreigners_liquidity` with `MIN_FOREIGN_NET_AMOUNT_KRW` (env `FOREIGNERS_MIN_NET_AMOUNT_KRW`, default 100000000), `MIN_MARKET_CAP_KRW` (env `FOREIGNERS_MIN_MARKET_CAP_KRW`, default 30000000000), `is_foreigners_ranking`, `_fetch_market_cap_maps`, `apply_market_cap_backfill`, `backfill_foreigners_market_cap`, `filter_illiquid_foreigners`; `get_top_stocks_impl(..., include_illiquid: bool = False)`; per-row `market_cap_source` ∈ `{kis_payload, fundamentals_snapshot, shares_outstanding_x_price, None}`; response `liquidity_filter` metadata dict.

**Notes:** Decoupled from Task 5 by the `foreign_net_amount`→`trade_amount` fallback and the three-spelling `is_foreigners_ranking`, but lands AFTER Task 5. B2 does NOT touch `supported_combinations` (Task 5 owns it). The robust filter signal is `|foreign_net_amount|` (KRW, always present in the KIS payload), NOT the null-prone market_cap; the market_cap floor is an OPTIONAL secondary gate applied ONLY where cap is known (null cap never excludes); `abs()` handles both net-buy (+) and net-sell (−). `_fetch_market_cap_maps` mirrors ROB-512's `_sector_labels_for_page`/`_rsi_by_symbol` (one session, two batched `.in_()` selects, fail-open → `({}, {})`). Env constants read at import time via `os.getenv` (matches `binance/backfill.py:36-38`); operator-tunable. Migration 0 — `market_cap` and `shares_outstanding` columns already exist; this is read-only enrichment + a new repository read method. Additive response keys: `liquidity_filter` dict + per-row `market_cap_source`; default-ON filter changes foreigners row membership (intended). Non-foreigners rankings untouched (block gated on `market == "kr" and is_foreigners_ranking(ranking_type)`). **Existing-test hermeticity:** the three foreigners tests already rewritten by Task 5 (220/327/907) now exercise the new block which calls `_fetch_market_cap_maps` (DB). Although it is fail-open, monkeypatch `foreigners_liquidity._fetch_market_cap_maps` to an async no-op returning `({}, {})` in each (or add an autouse fixture in that module) so they stay hermetic/fast; their `frgn_ntby_tr_pbmn` values (4e11, 3.6e11, 7e14) all clear the 1e8 threshold so row-count assertions stay valid; the 900210 fallback row carries `hts_avls` (kept from Task 5? — note: Task 5 removed `hts_avls` only from the named-fields test at 907, the 900210 fallback mock does not carry it, so its `market_cap` is null → `market_cap_source=None`, an additive key, no existing assertion breaks). **The new `TestForeignersLiquidity` handler mocks define `async def foreign_buying_rank(self, market, limit)` WITHOUT `rank_sort` — after Task 5 the dispatch passes `rank_sort=`, which raises `TypeError`; add `rank_sort="0"` to each of those four mock signatures when implementing on top of Task 5.** Degraded convention reuses `status`/`degraded_reason`; `truncated_for_size` is NOT applicable here.

- [ ] **Step 1: Write the failing test** — (1) create `tests/test_foreigners_liquidity.py`:

```python
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.mcp_server.tooling import foreigners_liquidity as fl
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)


# --------------------------------------------------------------------------
# Pure backfill
# --------------------------------------------------------------------------
class TestApplyMarketCapBackfill:
    def test_backfilled_from_snapshot(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={"005930": Decimal("400000000000000")},
            shares_map={},
        )
        assert rows[0]["market_cap"] == 4e14
        assert rows[0]["market_cap_source"] == "fundamentals_snapshot"

    def test_fallback_shares_times_price(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={},  # no snapshot
            shares_map={"005930": Decimal("6000000000")},
        )
        assert rows[0]["market_cap"] == pytest.approx(6_000_000_000 * 80000.0)
        assert rows[0]["market_cap_source"] == "shares_outstanding_x_price"

    def test_honest_null_when_both_missing(self):
        rows = [{"symbol": "999999", "price": 1000.0, "market_cap": None}]
        fl.apply_market_cap_backfill(rows, snapshot_caps={}, shares_map={})
        assert rows[0]["market_cap"] is None
        assert rows[0]["market_cap_source"] is None

    def test_no_fabrication_when_price_missing(self):
        rows = [{"symbol": "005930", "price": None, "market_cap": None}]
        fl.apply_market_cap_backfill(
            rows, snapshot_caps={}, shares_map={"005930": Decimal("6000000000")}
        )
        assert rows[0]["market_cap"] is None
        assert rows[0]["market_cap_source"] is None

    def test_keeps_existing_kis_payload_market_cap(self):
        rows = [{"symbol": "005930", "price": 80000.0, "market_cap": 1.23e14}]
        fl.apply_market_cap_backfill(
            rows,
            snapshot_caps={"005930": Decimal("9e14")},
            shares_map={},
        )
        assert rows[0]["market_cap"] == 1.23e14  # unchanged
        assert rows[0]["market_cap_source"] == "kis_payload"


# --------------------------------------------------------------------------
# Pure filter
# --------------------------------------------------------------------------
class TestFilterIlliquidForeigners:
    def _rows(self):
        return [
            {"symbol": "005930", "foreign_net_amount": 4e11, "market_cap": 1e14},
            {"symbol": "JUNK1", "foreign_net_amount": 5_000_000.0, "market_cap": None},
        ]

    def test_filter_excludes_low_foreign_amount(self):
        kept, excluded = fl.filter_illiquid_foreigners(self._rows())
        assert excluded == 1
        assert [r["symbol"] for r in kept] == ["005930"]

    def test_include_illiquid_keeps_all(self):
        rows = self._rows()
        kept, excluded = fl.filter_illiquid_foreigners(rows, include_illiquid=True)
        assert excluded == 0
        assert len(kept) == 2

    def test_market_cap_floor_excludes_when_known_and_tiny(self):
        rows = [
            {"symbol": "MICRO", "foreign_net_amount": 5e11, "market_cap": 1e9},
        ]
        kept, excluded = fl.filter_illiquid_foreigners(
            rows, min_market_cap_krw=3e10
        )
        assert excluded == 1
        assert kept == []

    def test_market_cap_null_does_not_trigger_floor(self):
        rows = [
            {"symbol": "OK", "foreign_net_amount": 5e11, "market_cap": None},
        ]
        kept, excluded = fl.filter_illiquid_foreigners(rows)
        assert excluded == 0
        assert [r["symbol"] for r in kept] == ["OK"]

    def test_fallback_reads_trade_amount_key_pre_b1(self):
        # Before B1 the mapper emits trade_amount, not foreign_net_amount.
        rows = [{"symbol": "005930", "trade_amount": 4e11, "market_cap": None}]
        kept, excluded = fl.filter_illiquid_foreigners(rows)
        assert excluded == 0
        assert len(kept) == 1

    def test_negative_net_sell_amount_uses_magnitude(self):
        rows = [{"symbol": "005930", "foreign_net_amount": -4e11, "market_cap": None}]
        kept, _ = fl.filter_illiquid_foreigners(rows)
        assert [r["symbol"] for r in kept] == ["005930"]


# --------------------------------------------------------------------------
# Batched repository reader (mocked session)
# --------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        return self._results.pop(0)


@pytest.mark.asyncio
class TestMarketCapBySymbols:
    async def test_returns_non_null_caps_for_latest_partition(self):
        import datetime as dt

        session = _FakeSession(
            [
                _FakeResult(scalar=dt.date(2026, 6, 29)),
                _FakeResult(
                    rows=[
                        SimpleNamespace(symbol="005930", market_cap=Decimal("4e14")),
                        SimpleNamespace(symbol="000660", market_cap=None),
                    ]
                ),
            ]
        )
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        out = await repo.market_cap_by_symbols(["005930", "000660"])
        assert out == {"005930": Decimal("4e14")}  # null filtered out

    async def test_empty_symbols_short_circuits(self):
        session = _FakeSession([])
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        assert await repo.market_cap_by_symbols([]) == {}
        assert session.executed == 0

    async def test_no_partition_returns_empty(self):
        session = _FakeSession([_FakeResult(scalar=None)])
        repo = InvestKrFundamentalsSnapshotsRepository(cast(Any, session))
        assert await repo.market_cap_by_symbols(["005930"]) == {}
```

  (2) Add `TestForeignersLiquidity` to `tests/test_mcp_top_stocks.py` (same DummyMCP/build_tools harness):

```python
@pytest.mark.asyncio
class TestForeignersLiquidity:
    async def _patch_fetch(self, monkeypatch, snapshot_caps=None, shares=None):
        from decimal import Decimal as _D

        from app.mcp_server.tooling import foreigners_liquidity

        async def fake_fetch(symbols, *, session_factory=None):
            return (
                {k: _D(str(v)) for k, v in (snapshot_caps or {}).items()},
                {k: _D(str(v)) for k, v in (shares or {}).items()},
            )

        monkeypatch.setattr(
            foreigners_liquidity, "_fetch_market_cap_maps", fake_fetch
        )

    async def test_backfill_wired_from_snapshot(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch, snapshot_caps={"005930": 4e14})

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "prdy_ctrt": "1.0",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        row = result["rankings"][0]
        assert row["market_cap"] == 4e14
        assert row["market_cap_source"] == "fundamentals_snapshot"
        assert result["liquidity_filter"]["include_illiquid"] is False
        assert result["liquidity_filter"]["excluded_count"] == 0

    async def test_filter_excludes_junk_default_on(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)  # no caps -> null

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "005930",
                        "hts_kor_isnm": "삼성전자",
                        "stck_prpr": "80000",
                        "frgn_ntby_qty": "5000000",
                        "frgn_ntby_tr_pbmn": "400000000000",
                    },
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_qty": "1000",
                        "frgn_ntby_tr_pbmn": "300000",  # 30만 KRW — junk
                    },
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert [r["symbol"] for r in result["rankings"]] == ["005930"]
        assert result["rankings"][0]["rank"] == 1
        assert result["liquidity_filter"]["excluded_count"] == 1

    async def test_include_illiquid_keeps_all(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_tr_pbmn": "300000",
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](
            market="kr", ranking_type="foreigners", include_illiquid=True
        )
        assert len(result["rankings"]) == 1
        assert result["liquidity_filter"]["include_illiquid"] is True
        assert result["liquidity_filter"]["excluded_count"] == 0

    async def test_filter_empties_sets_degraded(self, monkeypatch):
        tools = build_tools()
        await self._patch_fetch(monkeypatch)

        class MockKISClient:
            async def foreign_buying_rank(self, market, limit, rank_sort="0"):
                return [
                    {
                        "stck_shrn_iscd": "900111",
                        "hts_kor_isnm": "잡주",
                        "stck_prpr": "300",
                        "frgn_ntby_tr_pbmn": "300000",  # below threshold
                    }
                ]

        monkeypatch.setattr(analysis_tool_handlers, "KISClient", MockKISClient)
        result = await tools["get_top_stocks"](market="kr", ranking_type="foreigners")
        assert result["rankings"] == []
        assert result["total_count"] == 0
        assert result["status"] == "degraded"
        assert "liquidity threshold" in result["degraded_reason"]
        assert result["liquidity_filter"]["excluded_count"] == 1
```

  (Note: each mock above already includes `rank_sort="0"` in its `foreign_buying_rank` signature — required because the Task-5 dispatch passes `rank_sort=`; without it the call raises `TypeError`.)

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_foreigners_liquidity.py "tests/test_mcp_top_stocks.py::TestForeignersLiquidity" -v`. Expect ModuleNotFoundError (`app.mcp_server.tooling.foreigners_liquidity` absent) + `market_cap_by_symbols`/`liquidity_filter`/`include_illiquid` missing.

- [ ] **Step 3: Write the implementation**.

  (3A) `app/services/invest_kr_fundamentals_snapshots/repository.py` — insert after `latest_partition()` (after line 82):

```python
    async def market_cap_by_symbols(
        self, symbols: list[str]
    ) -> dict[str, Decimal]:
        """ROB-629: latest-partition market_cap for the given KR symbols.

        Single batched query keyed by the most recent ``snapshot_date`` that
        has data for any of ``symbols`` (mirrors ``_rsi_by_symbol``'s
        latest-date + ``symbol.in_()`` precedent). Only non-null market_cap
        rows are returned — callers fall back / keep honest null otherwise.
        """
        if not symbols:
            return {}
        latest = (
            await self._session.execute(
                select(func.max(InvestKrFundamentalsSnapshot.snapshot_date)).where(
                    InvestKrFundamentalsSnapshot.symbol.in_(symbols)
                )
            )
        ).scalar_one_or_none()
        if latest is None:
            return {}
        rows = (
            await self._session.execute(
                select(
                    InvestKrFundamentalsSnapshot.symbol,
                    InvestKrFundamentalsSnapshot.market_cap,
                ).where(
                    InvestKrFundamentalsSnapshot.snapshot_date == latest,
                    InvestKrFundamentalsSnapshot.symbol.in_(symbols),
                )
            )
        ).all()
        return {
            row.symbol: row.market_cap
            for row in rows
            if row.market_cap is not None
        }
```

  (3B) create `app/mcp_server/tooling/foreigners_liquidity.py`:

```python
"""ROB-629 B2: market_cap backfill + liquidity filter for the KR foreigners
(foreign_net_buy / foreign_net_sell) ranking.

The KIS foreign-buying-rank payload reliably carries the foreign net-flow KRW
value (frgn_ntby_tr_pbmn -> foreign_net_amount) but usually OMITS market cap.
So we (1) backfill market_cap from invest_kr_fundamentals_snapshots, falling
back to shares_outstanding x price, honest null when neither is available; and
(2) drop clear-junk illiquid rows using the ALWAYS-PRESENT foreign_net_amount
as the primary signal (NOT the null-prone market_cap)."""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.shared import to_optional_float as _to_optional_float
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_kr_fundamentals_snapshots.repository import (
    InvestKrFundamentalsSnapshotsRepository,
)

logger = logging.getLogger(__name__)

# Operator-tunable. Foreign net-flow KRW magnitude below this == clear junk.
MIN_FOREIGN_NET_AMOUNT_KRW: float = float(
    os.getenv("FOREIGNERS_MIN_NET_AMOUNT_KRW", "100000000")  # 1억 KRW
)
# Optional market-cap floor, applied ONLY where market_cap is known (never
# excludes a row just because cap is null).
MIN_MARKET_CAP_KRW: float = float(
    os.getenv("FOREIGNERS_MIN_MARKET_CAP_KRW", "30000000000")  # 300억 KRW
)

_FOREIGNERS_RANKING_TYPES: frozenset[str] = frozenset(
    {"foreign_net_buy", "foreign_net_sell", "foreigners"}
)


def is_foreigners_ranking(ranking_type: str) -> bool:
    return (ranking_type or "").strip().lower() in _FOREIGNERS_RANKING_TYPES


def _row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or "").strip()


def _abs_foreign_amount(row: dict[str, Any]) -> float | None:
    """Magnitude of foreign net flow (KRW). Reads B1's ``foreign_net_amount``
    with fallback to the current ``trade_amount`` key (same frgn_ntby_tr_pbmn)."""
    raw = row.get("foreign_net_amount")
    if raw is None:
        raw = row.get("trade_amount")
    val = _to_optional_float(raw)
    return abs(val) if val is not None else None


async def _fetch_market_cap_maps(
    symbols: list[str],
    *,
    session_factory: Any = AsyncSessionLocal,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """(snapshot_market_caps, shares_outstanding_map) for ``symbols``.

    Snapshot caps come from the latest fundamentals partition; shares come from
    kr_symbol_universe only for symbols missing a snapshot cap. fail-open: any
    DB error returns ({}, {}) so the foreigners path never breaks on a DB hiccup
    (market_cap simply stays null)."""
    if not symbols:
        return {}, {}
    try:
        async with session_factory() as db:
            repo = InvestKrFundamentalsSnapshotsRepository(db)
            snapshot_caps = await repo.market_cap_by_symbols(symbols)
            need_fallback = [s for s in symbols if s not in snapshot_caps]
            shares_map: dict[str, Decimal] = {}
            if need_fallback:
                rows = (
                    await db.execute(
                        sa.select(
                            KRSymbolUniverse.symbol,
                            KRSymbolUniverse.shares_outstanding,
                        ).where(KRSymbolUniverse.symbol.in_(need_fallback))
                    )
                ).all()
                shares_map = {
                    r.symbol: r.shares_outstanding
                    for r in rows
                    if r.shares_outstanding is not None
                }
            return snapshot_caps, shares_map
    except Exception:  # noqa: BLE001 — fail-open, leave market_cap untouched
        logger.warning("foreigners market_cap fetch failed", exc_info=True)
        return {}, {}


def apply_market_cap_backfill(
    rows: list[dict[str, Any]],
    *,
    snapshot_caps: dict[str, Decimal],
    shares_map: dict[str, Decimal],
) -> None:
    """Pure in-place backfill of market_cap with provenance, NEVER fabricated.

    Precedence: keep existing (KIS payload) value -> fundamentals snapshot cap
    -> shares_outstanding x price -> honest null."""
    for row in rows:
        existing = row.get("market_cap")
        if existing is not None:
            row.setdefault("market_cap_source", "kis_payload")
            continue
        symbol = _row_symbol(row)
        cap = snapshot_caps.get(symbol)
        if cap is not None:
            row["market_cap"] = float(cap)
            row["market_cap_source"] = "fundamentals_snapshot"
            continue
        shares = shares_map.get(symbol)
        price = _to_optional_float(row.get("price"))
        if shares is not None and price is not None:
            row["market_cap"] = float(shares) * price
            row["market_cap_source"] = "shares_outstanding_x_price"
            continue
        row["market_cap"] = None
        row["market_cap_source"] = None


async def backfill_foreigners_market_cap(
    rows: list[dict[str, Any]],
    *,
    session_factory: Any = AsyncSessionLocal,
) -> None:
    """Bounded (top-N rows already clamped to <=50) batched market_cap backfill."""
    symbols = list(
        dict.fromkeys(s for r in rows if (s := _row_symbol(r)))
    )
    snapshot_caps, shares_map = await _fetch_market_cap_maps(
        symbols, session_factory=session_factory
    )
    apply_market_cap_backfill(
        rows, snapshot_caps=snapshot_caps, shares_map=shares_map
    )


def filter_illiquid_foreigners(
    rows: list[dict[str, Any]],
    *,
    include_illiquid: bool = False,
    min_foreign_net_amount_krw: float = MIN_FOREIGN_NET_AMOUNT_KRW,
    min_market_cap_krw: float | None = MIN_MARKET_CAP_KRW,
) -> tuple[list[dict[str, Any]], int]:
    """Default-ON liquidity filter. Robust signal = |foreign_net_amount| (KRW,
    always present), with an OPTIONAL market_cap floor applied only where cap is
    known. ``include_illiquid=True`` bypasses. Returns (kept_rows, excluded)."""
    if include_illiquid:
        return list(rows), 0
    kept: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        amount = _abs_foreign_amount(row)
        if amount is None or amount < min_foreign_net_amount_krw:
            excluded += 1
            continue
        cap = _to_optional_float(row.get("market_cap"))
        if (
            min_market_cap_krw is not None
            and cap is not None
            and cap < min_market_cap_krw
        ):
            excluded += 1
            continue
        kept.append(row)
    return kept, excluded
```

  (3C) `app/mcp_server/tooling/analysis_tool_handlers.py` — wire the handler:

  1. Add the import near the other tooling imports (top of file):

```python
from app.mcp_server.tooling import foreigners_liquidity
```

  2. Signature (replace lines 80-84):

```python
async def get_top_stocks_impl(
    market: str = "kr",
    ranking_type: str = "volume",
    limit: int = 20,
    include_illiquid: bool = False,
) -> dict[str, Any]:
```

  3. Insert the backfill+filter+degraded block AFTER the `data_state` block (after current line 204) and BEFORE the losers-empty check (current line 206):

```python
    # ROB-629 B2: foreigners liquidity backfill + default-ON liquidity filter.
    liquidity_filter_meta: dict[str, Any] | None = None
    if market == "kr" and foreigners_liquidity.is_foreigners_ranking(ranking_type):
        await foreigners_liquidity.backfill_foreigners_market_cap(rankings)
        kept, excluded = foreigners_liquidity.filter_illiquid_foreigners(
            rankings, include_illiquid=include_illiquid
        )
        liquidity_filter_meta = {
            "include_illiquid": include_illiquid,
            "min_foreign_net_amount_krw": (
                foreigners_liquidity.MIN_FOREIGN_NET_AMOUNT_KRW
            ),
            "min_market_cap_krw": foreigners_liquidity.MIN_MARKET_CAP_KRW,
            "excluded_count": excluded,
        }
        if not include_illiquid and rankings and not kept:
            # Filter emptied a non-empty list (e.g. off-hours / all-junk).
            # Honest degraded signal — never fabricate rows.
            return {
                "rankings": [],
                "total_count": 0,
                "market": market,
                "ranking_type": ranking_type,
                "timestamp": datetime.datetime.now(kst_tz).isoformat(),
                "source": source,
                **({"data_state": data_state} if data_state is not None else {}),
                "status": "degraded",
                "degraded_reason": (
                    f"all {excluded} foreign-flow row(s) fell below the liquidity "
                    f"threshold (foreign_net_amount >= "
                    f"{foreigners_liquidity.MIN_FOREIGN_NET_AMOUNT_KRW:.0f} KRW); "
                    "pass include_illiquid=true to bypass, or retry during market "
                    "hours when foreign net flow is non-trivial"
                ),
                "liquidity_filter": liquidity_filter_meta,
            }
        rankings = kept
        for new_rank, row in enumerate(rankings, start=1):
            row["rank"] = new_rank
```

  4. Attach metadata to the normal response (after current line 229 `response["data_state"] = data_state`):

```python
    if liquidity_filter_meta is not None:
        response["liquidity_filter"] = liquidity_filter_meta
    return response
```

  (3D) `app/mcp_server/tooling/analysis_registration.py` — replace the `get_top_stocks` wrapper (lines 91-100):

```python
    async def get_top_stocks(
        market: str = "kr",
        ranking_type: str = "volume",
        limit: int = 20,
        include_illiquid: bool = False,
    ) -> dict[str, Any]:
        return await get_top_stocks_impl(
            market=market,
            ranking_type=ranking_type,
            limit=limit,
            include_illiquid=include_illiquid,
        )
```

  Also extend the tool description to mention the `include_illiquid` default-ON liquidity filter + market_cap backfill provenance.

  (3E) Existing-test hermeticity: in `tests/test_mcp_top_stocks.py`, neutralize the DB call for the three foreigners tests rewritten in Task 5 (`test_kr_foreigners_ranking_fallback_to_mksc_shrn_iscd`, `test_kr_foreigners_routing`, `test_kr_foreigners_ranking_foreign_specific_fields`) by monkeypatching `foreigners_liquidity._fetch_market_cap_maps` to an async no-op returning `({}, {})` (or add an autouse fixture scoped to those tests). Their `frgn_ntby_tr_pbmn` values clear the 1e8 threshold, so row-count assertions stay valid; `market_cap_source` is an additive key.

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/test_foreigners_liquidity.py tests/test_mcp_top_stocks.py -v`. Expect all PASS.

- [ ] **Step 5: Run gate** — `uv run ruff check . && uv run ty check app/`

- [ ] **Step 6: Commit** — `git add app/services/invest_kr_fundamentals_snapshots/repository.py app/mcp_server/tooling/foreigners_liquidity.py app/mcp_server/tooling/analysis_tool_handlers.py app/mcp_server/tooling/analysis_registration.py tests/test_foreigners_liquidity.py tests/test_mcp_top_stocks.py && git commit` with message ending in the two trailers:

```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F2treu74Jiun6cMrTSttN2
```

---

## Operator 장중 검증 게이트 (PR-B 머지 전)

PR-B must NOT merge until an operator runs a real KIS call during KRX regular session (09:00–15:30 KST) and confirms all three (spec §D):

- [ ] **(a) net-sell direction** — `get_top_stocks(market="kr", ranking_type="foreign_net_sell")` (FID `FID_RANK_SORT_CLS_CODE='1'`) returns the actual 순매도 (net-sell) leaders, not the net-buy list. Cross-check `foreign_net_qty`/`foreign_net_amount` are negative/sell-side as expected.
- [ ] **(b) liquidity filter** — the default-ON filter excludes clear junk (잡주, sub-1억 KRW foreign net flow) and surfaces large-caps; spot-check that a known large-cap (e.g. 삼성전자 005930) is present and a known micro-cap is excluded. Confirm `liquidity_filter.excluded_count` is plausible and `include_illiquid=true` restores the full list.
- [ ] **(c) named-field value sanity** — `foreign_net_qty` (`frgn_ntby_qty`) and `foreign_net_amount` (`frgn_ntby_tr_pbmn`) carry sane magnitudes; `market_cap` is backfilled where available with a `market_cap_source` of `fundamentals_snapshot` / `shares_outstanding_x_price` / `kis_payload`, and honest `null` (source `None`) where neither source has data — never fabricated.

Env-tunable thresholds note: operators can retune without code change via `FOREIGNERS_MIN_NET_AMOUNT_KRW` (default `100000000` = 1억 KRW) and `FOREIGNERS_MIN_MARKET_CAP_KRW` (default `30000000000` = 300억 KRW), both read at import time. Document the chosen values + the verification result in the ROB-629 runbook.

## Follow-ups (out of scope)

- **Pagination (cursor) for news**: truncation + structural de-dup + size cap nearly resolve the size problem, so pagination is deferred. When implemented, port the `screen_stocks_snapshot` (ROB-465) offset pattern (`get_news_articles` already supports offset at `llm_news_service.py:156,207`; `build_market_issues` slices the ranked `meaningful` list). NOTE: `get_top_stocks` has NO cursor today (the issue's claim was wrong) — the real precedent is `screen_stocks_snapshot`.
- **Briefing-list full-universe symbol tagging**: the deterministic symbol-tagging stack (`news_entity_matcher.match_symbols_for_article`) exists and is used by the web feed + `get_market_issues`, but the briefing MCP path is unwired; KR alias dictionary is only ~15 entries (삼성/하이닉스/NAVER/기아 covered; 한화에어로 012450 NOT) — broaden as a separate follow-up.
- **`get_news` KR inline summary/sentiment**: blocked on the in-process LLM ownership boundary (no in-proc LLM synthesis) + the ROB-491 judgment Job (scheduleless). Use `relevance.price_relevance` as the catalyst signal instead.
- **ROB-626 외인소진율 enrichment of top-N foreigners rows**: ROB-626's `build_confirmed_block` is per-symbol Naver scrape (cross-symbol ranking impossible), so it is NOT a ranking source — reuse only as an OPTIONAL top-N enrichment (외인소진율 등) follow-up.
