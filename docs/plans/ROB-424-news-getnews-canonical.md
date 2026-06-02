# ROB-424 — get_news canonical symbol-news evidence; relabel/remove legacy broad news surfaces — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `get_news`/`symbol_news_service` the single canonical symbol-level news evidence path, harden the remaining DB-first decision path (`research_pipeline` NewsStage), and demote/remove the legacy broad-DB news surfaces so none of them can implicitly feed a buy/sell judgment.

**Architecture:** Invert `NewsStageAnalyzer` to fetch on-demand via `symbol_news_service` (drop the `news_articles` DB read + write-back); remove the dead `search_news` MCP tool and relabel `get_market_news` as legacy-briefing-only; relabel the news-dimension evidence `source` to reflect its true on-demand origin; delete the now-unused `research_news_service` shim. Migration 0, broker/order/watch/scheduler mutation 0, no `news_articles` row deletion.

**Tech Stack:** Python 3.13, FastMCP, SQLAlchemy (async), pytest, ruff, ty, uv.

---

## Spec Recap

### Why
ROB-423 (#1106, merged) moved the snapshot-backed `invest_report` news citations to on-demand `get_news`/`symbol_news_service`. A **second** decision path remained DB-first: `research_pipeline` (`POST /api/research-pipeline/sessions`) → `NewsStageAnalyzer` read the broad `news_articles` DB first to compute a BULL/BEAR/NEUTRAL sentiment verdict feeding `ResearchSummary` + legacy `StockAnalysisResult`. So the broad `news-ingestor` firehose could still implicitly tilt a buy/sell verdict. Separately, `get_market_news`/`search_news` MCP tools read the broad DB; `search_news` has zero consumers, `get_market_news` is the entry-point of the documented pre-market briefing pipeline.

### Decisions (locked with user)
- **D1 — NewsStage on-demand-first.** Invert NewsStage so `symbol_news_service.fetch_symbol_news` is the only news source; no `news_articles` read, no write-back.
- **D2 — Split tool treatment.** **Remove** `search_news` (truly dead: zero refs outside tests). **Relabel** `get_market_news` as legacy-briefing-only (keep registered — it feeds the pre-market briefing pipeline).
- **D3 — Relabel** `news_evidence.py` snapshot `source` `"news_articles"` → `"symbol_news"` (accurate provenance; the data is on-demand).
- **D4 — Delete** the `research_news_service` shim (only NewsStage used it; after D1 it has no consumer).
- **D5 — Docs/audit.** Mark `news-ingestor` broad-feed-only (not a report freshness/citation source); ship this consumer audit in the PR.

### Verified facts (adversarial verification, 2026-06-02)
- `symbol_news_service.fetch_symbol_news(symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0)` is **fail-soft** (never raises) → returns `SymbolNewsFetchResult(status: str ∈ {"ok","empty","unavailable","error"}, articles: list[SymbolNewsArticle])`. `SymbolNewsArticle` has `.title` and `.published_at`; **no** `.keywords`/`.article_published_at`. (`app/services/symbol_news_service.py:31-55,158-203`)
- `research_pipeline` is **default-off**: `RESEARCH_PIPELINE_ENABLED`, `RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED`, `RESEARCH_PIPELINE_DUAL_WRITE_ENABLED` all default `False` (`app/core/config.py:659-661`). The NewsStage behavior change is therefore low live-traffic risk.
- `get_news_articles_with_fallback` is used only by `news_stage.py` (prod) + `tests/test_news_stage_fallback.py` + `tests/test_news_stage_on_demand.py`. KEEP the function in `llm_news_service` (nothing else needs deleting); `tests/test_news_stage_fallback.py` exercises it **directly** (not NewsStage) and stays green untouched.
- `NormalizedArticle` is defined **only** in `research_news_service.py:21`; after D1, NewsStage consumes `SymbolNewsArticle` directly and won't need it.
- No production code branches on the news-evidence `source` string (relabel is safe). `NewsSnapshotCollector` never reads broad `news_articles` (only `symbol_news_service` + `research_reports`). Watch/order-intent never touch news. `invest_coverage_service._news_surfaces` reads `news_articles` only for a freshness **dashboard** (not a decision; out of scope).
- `get_market_news` is referenced as an active step in `docs/pre-market-news-briefing-pipeline.md:18,109-120,212` (agent/OpenClaw-facing → no Python caller). Hence relabel, not remove.

### Consumer/writer audit (ship in PR description)

| Surface | Source | Decision relevance | Treatment |
|---|---|---|---|
| `get_news` MCP (`fundamentals/_news.py:47`) | on-demand KR Naver / US+Crypto Finnhub | citation primary | **keep (canonical)** |
| snapshot report (`collectors/news.py`, `news_evidence.py`) | on-demand `symbol_news_service` (+research_reports fallback) | dimension evidence | **keep (ROB-423-aligned)** |
| `NewsStageAnalyzer` (`news_stage.py`) | broad `news_articles` DB-first | research_pipeline verdict → `ResearchSummary`/`StockAnalysisResult` | **migrate → on-demand (D1)** |
| `get_market_news` MCP (`news_handlers.py:227`) | broad `news_articles` DB | pre-market briefing (agent) | **relabel legacy-briefing (D2)** |
| `search_news` MCP (`news_handlers.py:256`) | broad `news_articles` DB | none (tests only) | **remove (D2)** |
| `get_market_issues` MCP (`news_handlers.py:267`, ROB-130) | broad `news_articles` DB | intentional clustering | **out of scope / do not touch** |
| `research_news_service` shim | wraps `symbol_news_service` | NewsStage only | **delete (D4)** |
| `news_evidence.py:135` `source="news_articles"` | on-demand snapshot articles | provenance label | **relabel `symbol_news` (D3)** |
| `invest_coverage_service._news_surfaces` | broad `news_articles` count | dashboard freshness, not a decision | **out of scope / keep** |

### Acceptance Criteria
1. `NewsStageAnalyzer` reads no `news_articles` DB and calls only `symbol_news_service.fetch_symbol_news`; no `bulk_create_news_articles` write-back (code + test).
2. Provider `status` ok/empty/error → verdict BULL·BEAR·NEUTRAL / NEUTRAL(0 headlines) / UNAVAILABLE (test).
3. `NEWS_TOOL_NAMES` lacks `search_news`, keeps `get_market_news` + `get_market_issues` (regression guard test).
4. `get_market_news` MCP description + response carry an explicit "legacy broad-market briefing surface; NOT investment-decision evidence" notice.
5. `news_evidence` snapshot-articles path `data_health.source == "symbol_news"`.
6. `research_news_service.py` + its test deleted; zero residual imports of the module or `NormalizedArticle`.
7. `get_news` envelope regression test (`tests/mcp_server/tooling/test_get_news_envelope.py`) passes unchanged.
8. Zero new public MCP news tool; zero production scheduler change; zero `news_articles` row deletion; zero migration.
9. `uv run ruff check app/ tests/`, `uv run ruff format --check app/ tests/`, ty, and the affected test suites are green.

### Out of Scope / Do Not Touch
- `get_market_issues` (ROB-130) — broad DB but intentional; unchanged.
- `tests/test_news_stage_fallback.py` — tests the retained `llm_news_service` function directly; do not edit.
- `news_evidence.py:77` `source="research_reports"` label and the research_reports fallback path — unchanged.
- `tests/.../test_hermes_context_news_dimension.py:137` snapshot **payload** `source` field — it is the snapshot wrapper's own metadata, independent of `data_health.source`; leave it.
- `llm_news_service.get_news_articles*` deletion (kept for `get_market_news`, n8n, `get_market_issues`); `invest_coverage_service` news count; production Prefect pause/unpause (operator-gated); ROB-423 citation re-impl; new providers; mass crawl/backfill; broker/order/watch/order-intent mutation; live trading.

---

## File Structure / Change Map

| File | Change |
|---|---|
| `app/analysis/stages/news_stage.py` | Rewrite to on-demand-first; drop DB read/write-back, `MIN_DB_ARTICLES_BEFORE_ON_DEMAND_FETCH`, `_OnDemandArticlePayload`, `_to_persist_payloads`; import `symbol_news_service` |
| `tests/test_news_stage_on_demand.py` | Rewrite for on-demand-first contract (status→verdict, no DB) |
| `app/mcp_server/tooling/news_handlers.py` | `NEWS_TOOL_NAMES` drop `search_news`; delete `search_news` tool + `_search_news_impl`/`_search_news_db`; relabel `get_market_news` description + response |
| `tests/test_news_rss.py` | Update `test_news_tool_names_exported`; delete `_search_news*` tests; add `get_market_news` legacy-notice assertion |
| `app/services/investment_dimensions/news_evidence.py` | `:135` `source="news_articles"` → `"symbol_news"` |
| `tests/services/investment_dimensions/test_news_evidence.py` | `:175`, `:200` assert `"symbol_news"` |
| `tests/services/investment_stages/test_hermes_context_news_dimension.py` | `:191` assert `"symbol_news"` |
| `app/services/research_news_service.py` | **Delete** |
| `tests/services/test_research_news_service.py` | **Delete** |
| `app/mcp_server/README.md` | Remove `search_news` section; relabel `get_market_news` section legacy |
| `docs/pre-market-news-briefing-pipeline.md` | Note `get_market_news` legacy-briefing-only (kept); `search_news` removed |
| `docs/runbooks/news-ingestor-kr-scheduled-push.md` | Note broad-feed-only; not a report freshness/citation source |

**Task order:** Task 1 (NewsStage) → Task 4 (delete shim) must be sequential (NewsStage must stop importing the shim first). Tasks 2, 3 are independent. Task 5 (docs + full verify) last.

---

## Task 1: NewsStage on-demand-first rewrite

**Files:**
- Modify: `app/analysis/stages/news_stage.py`
- Test: `tests/test_news_stage_on_demand.py` (full rewrite)
- Unchanged (must still pass): `tests/analysis/stages/test_news_stage.py`

- [ ] **Step 1: Rewrite the on-demand test to express the new contract (failing)**

Replace the entire contents of `tests/test_news_stage_on_demand.py` with:

```python
"""ROB-424 — NewsStageAnalyzer on-demand-first behavior (get_news canonical)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.analysis.stages import news_stage
from app.analysis.stages.base import StageContext
from app.analysis.stages.news_stage import NewsStageAnalyzer
from app.schemas.research_pipeline import StageVerdict
from app.services.symbol_news_service import SymbolNewsArticle, SymbolNewsFetchResult


def _article(*, title: str, published_at: datetime | None = None) -> SymbolNewsArticle:
    return SymbolNewsArticle(
        provider="finnhub",
        market="us",
        symbol="AMZN",
        external_article_id=None,
        title=title,
        source_name="Reuters",
        canonical_url="https://example.com/x",
        summary=None,
        published_at=published_at,
        fetched_at=datetime(2026, 5, 5, 13, 30, tzinfo=UTC),
    )


def _result(status: str, articles: list[SymbolNewsArticle]) -> SymbolNewsFetchResult:
    return SymbolNewsFetchResult(
        symbol="AMZN",
        market="us",
        provider="finnhub",
        status=status,
        requested_limit=20,
        returned_count=len(articles),
        articles=articles,
    )


def _ctx(symbol: str = "AMZN", instrument_type: str = "equity_us") -> StageContext:
    return StageContext(
        session_id=1,
        symbol=symbol,
        instrument_type=instrument_type,
        symbol_name="Amazon.com Inc.",
    )


class TestNewsStageOnDemandFirst:
    @pytest.mark.asyncio
    async def test_no_broad_db_seam_on_module(self) -> None:
        # ROB-424 AC1: the broad-DB helpers must no longer be imported here.
        assert not hasattr(news_stage, "get_news_articles_with_fallback")
        assert not hasattr(news_stage, "bulk_create_news_articles")

    @pytest.mark.asyncio
    async def test_ok_positive_headline_is_bull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = AsyncMock(
            return_value=_result(
                "ok",
                [
                    _article(
                        title="Amazon earnings beat soaring growth",
                        published_at=datetime(2026, 5, 5, 13, 0, tzinfo=UTC),
                    )
                ],
            )
        )
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)

        out = await NewsStageAnalyzer().analyze(_ctx())

        fetch.assert_awaited_once()
        assert fetch.await_args.args[0] == "AMZN"
        assert fetch.await_args.args[1] == "us"
        assert out.signals.headline_count == 1
        assert out.verdict == StageVerdict.BULL

    @pytest.mark.asyncio
    async def test_empty_status_is_neutral_zero_headlines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            news_stage, "fetch_symbol_news", AsyncMock(return_value=_result("empty", []))
        )
        out = await NewsStageAnalyzer().analyze(_ctx())
        assert out.verdict == StageVerdict.NEUTRAL
        assert out.signals.headline_count == 0

    @pytest.mark.asyncio
    async def test_error_status_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            news_stage, "fetch_symbol_news", AsyncMock(return_value=_result("error", []))
        )
        out = await NewsStageAnalyzer().analyze(_ctx())
        assert out.verdict == StageVerdict.UNAVAILABLE
        assert out.confidence == 0

    @pytest.mark.asyncio
    async def test_kr_routes_to_kr_market(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch = AsyncMock(return_value=_result("empty", []))
        monkeypatch.setattr(news_stage, "fetch_symbol_news", fetch)
        await NewsStageAnalyzer().analyze(_ctx(symbol="005930", instrument_type="equity_kr"))
        assert fetch.await_args.args[1] == "kr"
```

- [ ] **Step 2: Run the new test to confirm it fails**

Run: `uv run pytest tests/test_news_stage_on_demand.py -v`
Expected: FAIL — `ImportError`/`AttributeError` (e.g. `SymbolNewsFetchResult` import OK, but `news_stage.fetch_symbol_news` is still the shim and `get_news_articles_with_fallback` still on the module → `test_no_broad_db_seam_on_module` fails).

- [ ] **Step 3: Rewrite `app/analysis/stages/news_stage.py`**

Replace the entire file with (note `_compute_signals_from_articles` is preserved verbatim):

```python
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.analysis.stages.base import BaseStageAnalyzer, StageContext
from app.core.timezone import now_kst_naive
from app.schemas.research_pipeline import (
    NewsSignals,
    SourceFreshness,
    StageOutput,
    StageVerdict,
)
from app.services.symbol_news_service import SymbolNewsArticle, fetch_symbol_news

logger = logging.getLogger(__name__)


def _market_from_instrument(instrument_type: str) -> str:
    if instrument_type == "equity_us":
        return "us"
    if instrument_type == "crypto":
        return "crypto"
    return "kr"


@dataclass
class _SignalArticle:
    """Minimal article shape consumed by _compute_signals_from_articles."""

    title: str
    article_published_at: datetime | None
    keywords: list[str]


def _to_signal_articles(articles: list[SymbolNewsArticle]) -> list[_SignalArticle]:
    # SymbolNewsArticle carries no keyword field; KR Naver / US+Crypto Finnhub
    # on-demand items provide none, so themes stay empty (already true for the
    # prior on-demand fallback path).
    return [
        _SignalArticle(
            title=a.title,
            article_published_at=a.published_at,
            keywords=[],
        )
        for a in articles
    ]


async def _fetch_recent_headlines(
    symbol: str,
    instrument_type: str,
) -> dict[str, Any]:
    """On-demand-first headlines via get_news/symbol_news_service (ROB-424).

    The broad ``news_articles`` DB is no longer read or written here, so the
    research-pipeline news verdict cannot be driven by the broad ingestor feed.
    The provider seam is fail-soft; ``status`` is carried in the returned dict so
    ``analyze`` can tell a provider error/unavailable from a genuine empty window.
    """
    market = _market_from_instrument(instrument_type)
    result = await fetch_symbol_news(symbol, market, limit=20)
    signals = _compute_signals_from_articles(_to_signal_articles(result.articles))
    signals["status"] = result.status
    return signals


def _compute_signals_from_articles(articles: list[Any]) -> dict[str, Any]:
    """Pure logic to compute sentiment/signals from a list of articles."""
    if not articles:
        return {
            "headlines": [],
            "headline_count": 0,
            "sentiment_score": 0.0,
            "top_themes": [],
            "urgent_flags": [],
            "newest_age_minutes": 0,
        }

    sentiments = []
    themes = []
    newest_dt = None

    # V1 Rule-based sentiment keywords
    POS_KEYWORDS = {
        "상승",
        "호재",
        "급등",
        "매수",
        "수익",
        "성장",
        "실적발표",
        "흑자",
        "soaring",
        "positive",
        "bullish",
        "buy",
        "growth",
        "earnings",
        "beat",
        "outperform",
    }
    NEG_KEYWORDS = {
        "하락",
        "악재",
        "급락",
        "매도",
        "손실",
        "위기",
        "적자",
        "전망하치",
        "falling",
        "negative",
        "bearish",
        "sell",
        "loss",
        "crisis",
        "miss",
        "underperform",
    }

    for article in articles:
        # Freshness
        if article.article_published_at:
            if newest_dt is None or article.article_published_at > newest_dt:
                newest_dt = article.article_published_at

        # Sentiment scoring (v1: keyword-based)
        score = 0.0
        title_lower = article.title.lower()
        if any(kw in title_lower for kw in POS_KEYWORDS):
            score += 0.5
        if any(kw in title_lower for kw in NEG_KEYWORDS):
            score -= 0.5

        # Cap score
        score = max(-1.0, min(1.0, score))
        sentiments.append(score)

        # Themes from keywords
        if article.keywords:
            # article.keywords is list or JSONB
            if isinstance(article.keywords, list):
                themes.extend(article.keywords)

    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

    # Dedupe and limit themes
    unique_themes = []
    for t in themes:
        if t not in unique_themes:
            unique_themes.append(t)
    top_themes = unique_themes[:10]

    # Freshness calculation
    newest_age_minutes = 0
    if newest_dt:
        now = now_kst_naive()
        diff = now - newest_dt.replace(tzinfo=None)
        newest_age_minutes = max(0, int(diff.total_seconds() / 60))

    return {
        "headlines": [
            {"title": a.title, "published_at": a.article_published_at} for a in articles
        ],
        "headline_count": len(articles),
        "sentiment_score": round(avg_sentiment, 2),
        "top_themes": top_themes,
        "urgent_flags": [],
        "newest_age_minutes": newest_age_minutes,
    }


class NewsStageAnalyzer(BaseStageAnalyzer):
    stage_type = "news"

    async def analyze(self, ctx: StageContext) -> StageOutput:
        try:
            raw = await _fetch_recent_headlines(ctx.symbol, ctx.instrument_type)
        except Exception as exc:  # defensive — symbol_news_service is fail-soft
            logger.error(f"News analysis failed for {ctx.symbol}: {exc}")
            return self._unavailable()

        if raw.get("status") in ("error", "unavailable"):
            logger.info(
                "news_stage: provider status=%s for %s -> UNAVAILABLE",
                raw.get("status"),
                ctx.symbol,
            )
            return self._unavailable()

        signals = NewsSignals(
            headline_count=raw["headline_count"],
            sentiment_score=raw["sentiment_score"],
            top_themes=raw["top_themes"],
            urgent_flags=raw["urgent_flags"],
        )

        # Verdict mapping rule (status="ok"/"empty"):
        # BULL: sentiment_score > 0.15 and headline_count > 0
        # BEAR: sentiment_score < -0.15 and headline_count > 0
        # NEUTRAL: otherwise (includes empty window)
        verdict = StageVerdict.NEUTRAL
        if signals.headline_count > 0:
            if signals.sentiment_score > 0.15:
                verdict = StageVerdict.BULL
            elif signals.sentiment_score < -0.15:
                verdict = StageVerdict.BEAR

        return StageOutput(
            stage_type=self.stage_type,
            verdict=verdict,
            confidence=65,  # Moderate confidence for news stage
            signals=signals,
            snapshot_at=datetime.now(UTC),
            source_freshness=SourceFreshness(
                newest_age_minutes=raw["newest_age_minutes"],
                oldest_age_minutes=0,
                source_count=1,
            ),
        )

    def _unavailable(self) -> StageOutput:
        return StageOutput(
            stage_type=self.stage_type,
            verdict=StageVerdict.UNAVAILABLE,
            confidence=0,
            signals=NewsSignals(
                headline_count=0,
                sentiment_score=0.0,
                top_themes=[],
                urgent_flags=[],
            ),
            snapshot_at=datetime.now(UTC),
        )
```

- [ ] **Step 4: Run NewsStage tests to verify they pass**

Run: `uv run pytest tests/test_news_stage_on_demand.py tests/analysis/stages/test_news_stage.py -v`
Expected: PASS — new on-demand-first tests green; the two pre-existing `test_news_stage.py` tests still pass (they patch `_fetch_recent_headlines`; the bull test's dict has no `status` → `raw.get("status")` is `None` → not error/unavailable → BULL; the unavailable test raises → caught → UNAVAILABLE).

- [ ] **Step 5: Commit**

```bash
git add app/analysis/stages/news_stage.py tests/test_news_stage_on_demand.py
git commit -m "feat(ROB-424): NewsStage on-demand-first via symbol_news_service (drop broad news_articles DB)"
```

---

## Task 4: Delete the `research_news_service` shim

**Runs after Task 1** (NewsStage no longer imports the shim; the rewritten `test_news_stage_on_demand.py` no longer imports `NormalizedArticle`).

**Files:**
- Delete: `app/services/research_news_service.py`
- Delete: `tests/services/test_research_news_service.py`

- [ ] **Step 1: Confirm zero residual importers (must be empty)**

Run: `rg -n "research_news_service|NormalizedArticle" app/ tests/`
Expected: only matches inside the two files about to be deleted. If any other file matches, STOP and migrate it first (none expected per verification).

- [ ] **Step 2: Delete the shim and its test**

```bash
git rm app/services/research_news_service.py tests/services/test_research_news_service.py
```

- [ ] **Step 3: Re-confirm zero residual references**

Run: `rg -n "research_news_service|NormalizedArticle" app/ tests/`
Expected: no output.

- [ ] **Step 4: Run NewsStage + import-sanity check**

Run: `uv run pytest tests/test_news_stage_on_demand.py tests/analysis/stages/test_news_stage.py tests/test_news_stage_fallback.py -v`
Expected: PASS (incl. `test_news_stage_fallback.py`, which tests the retained `llm_news_service` function directly and is untouched).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(ROB-424): delete research_news_service shim (NewsStage now calls symbol_news_service directly)"
```

---

## Task 2: Remove `search_news`; relabel `get_market_news` legacy-briefing-only

**Files:**
- Modify: `app/mcp_server/tooling/news_handlers.py`
- Test: `tests/test_news_rss.py`
- Docs: `app/mcp_server/README.md`, `docs/pre-market-news-briefing-pipeline.md`

- [ ] **Step 1: Update the tool-name + relabel tests (failing)**

In `tests/test_news_rss.py`, replace the body of `test_news_tool_names_exported` (currently `tests/test_news_rss.py:509-513`) with:

```python
    def test_news_tool_names_exported(self):
        from app.mcp_server.tooling.news_handlers import NEWS_TOOL_NAMES

        # ROB-424: search_news removed; get_market_news kept (legacy briefing).
        assert "search_news" not in NEWS_TOOL_NAMES
        assert "get_market_news" in NEWS_TOOL_NAMES
        assert "get_market_issues" in NEWS_TOOL_NAMES
```

Then **delete** every test function in `tests/test_news_rss.py` that references `_search_news_impl` or `_search_news_db` (identify them with `rg -n "_search_news" tests/test_news_rss.py`; per verification these are `test_search_news_calls_service`, `test_search_news_db_builds_valid_jsonb_query`, `test_search_news_impl_with_keyword_returns_result`, and the others at the `_search_news`-matching lines).

Add a new test asserting the `get_market_news` legacy notice (place near the other `_get_market_news_impl` tests):

```python
    @pytest.mark.asyncio
    async def test_get_market_news_carries_legacy_surface_notice(self, monkeypatch):
        from app.mcp_server.tooling import news_handlers

        async def _empty(**kwargs):
            return []

        monkeypatch.setattr(news_handlers, "get_news_articles", AsyncMock(side_effect=_empty))
        result = await news_handlers._get_market_news_impl(hours=24, limit=20)
        assert result["surface"] == "legacy_market_briefing"
        assert "investment-decision evidence" in result["advisory"]
```

(Match the existing import style for `AsyncMock` already used in `tests/test_news_rss.py`.)

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_news_rss.py -v`
Expected: FAIL — `test_news_tool_names_exported` (search_news still present) and the new legacy-notice test (`surface` key absent).

- [ ] **Step 3: Edit `app/mcp_server/tooling/news_handlers.py`**

3a. `news_handlers.py:26` — drop `search_news`:

```python
NEWS_TOOL_NAMES = ["get_market_news", "get_market_issues"]
```

3b. In `_get_market_news_impl`, add the legacy notice to the returned dict. The function currently returns a dict with `count`/`total`/`news` (and `market`/`sources`/`feed_sources`). Add these two keys to **every** `return` dict in `_get_market_news_impl`:

```python
        "surface": "legacy_market_briefing",
        "advisory": (
            "Legacy broad-market DB-backed surface for briefing only; "
            "NOT investment-decision evidence. Use get_news for symbol-level decisions."
        ),
```

3c. Relabel the `@mcp.tool(name="get_market_news", description=...)` text (`news_handlers.py:227-235`) — prepend the legacy marker:

```python
        description=(
            "[LEGACY: broad market DB-backed briefing surface; NOT investment-decision "
            "evidence — use get_news for symbol-level decisions] "
            "Get recent market news. Supports filtering by market, publisher (source), "
            "collection path (feed_source), and keyword. Returns both publisher names "
            "and collection paths for briefing segmentation. briefing_filter=True "
            "formats market-specific sections for kr/us and ranks crypto-relevant "
            "items while separating broad-tech noise."
        ),
```

3d. Delete the `search_news` MCP tool block (`news_handlers.py:256-265`, the `@mcp.tool(name="search_news")` decorator + `async def search_news(...)`).

3e. Delete the now-orphaned `_search_news_impl` (`news_handlers.py:207-223`) and `_search_news_db` (`news_handlers.py:174-204`) functions, and remove imports that become unused (e.g. `from sqlalchemy import cast, func, or_, select` and `JSONB` — keep only what `_get_market_news_impl`/`_article_to_dict`/`get_market_issues` still use; run ruff to confirm).

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_news_rss.py tests/test_mcp_news_crypto_relevance.py tests/test_market_news_briefing_formatter.py -v`
Expected: PASS.

- [ ] **Step 5: Update docs**

In `app/mcp_server/README.md`: delete the `search_news(...)` section (`:39-41`); prepend `[LEGACY — briefing only, not decision evidence]` to the `get_market_news(...)` section heading (`:29`).

In `docs/pre-market-news-briefing-pipeline.md`: add a one-line note that `get_market_news` is a legacy broad-market briefing surface (still supported for this pipeline) and that `search_news` has been removed.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/news_handlers.py tests/test_news_rss.py app/mcp_server/README.md docs/pre-market-news-briefing-pipeline.md
git commit -m "feat(ROB-424): remove dead search_news MCP tool; relabel get_market_news legacy-briefing-only"
```

---

## Task 3: Relabel news-dimension evidence source

**Files:**
- Modify: `app/services/investment_dimensions/news_evidence.py`
- Test: `tests/services/investment_dimensions/test_news_evidence.py`, `tests/services/investment_stages/test_hermes_context_news_dimension.py`

- [ ] **Step 1: Update assertions to expect `symbol_news` (failing)**

In `tests/services/investment_dimensions/test_news_evidence.py`, change both:
- `:175` `assert bundle["data_health"]["source"] == "news_articles"` → `== "symbol_news"`
- `:200` `assert bundle["data_health"]["source"] == "news_articles"` → `== "symbol_news"`

In `tests/services/investment_stages/test_hermes_context_news_dimension.py`:
- `:191` `assert news_ev["data_health"]["source"] == "news_articles"` → `== "symbol_news"`

Do NOT change `tests/.../test_hermes_context_news_dimension.py:137` (`"source": "news_articles"` inside `payload_json`) — that is the snapshot wrapper's own metadata, independent of `data_health.source`.

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/services/investment_dimensions/test_news_evidence.py tests/services/investment_stages/test_hermes_context_news_dimension.py -v`
Expected: FAIL — three assertions now expect `"symbol_news"` but code still emits `"news_articles"`.

- [ ] **Step 3: Edit `app/services/investment_dimensions/news_evidence.py:135`**

In `_evidence_from_articles`'s `_build_result(...)` call, change:

```python
        source="symbol_news",
```

(Leave the research_reports path at `:77` as `source="research_reports"`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/services/investment_dimensions/test_news_evidence.py tests/services/investment_stages/test_hermes_context_news_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/investment_dimensions/news_evidence.py tests/services/investment_dimensions/test_news_evidence.py tests/services/investment_stages/test_hermes_context_news_dimension.py
git commit -m "fix(ROB-424): label news-dimension evidence source as symbol_news (on-demand provenance)"
```

---

## Task 5: Docs (news-ingestor), audit, and full verification

**Files:**
- Modify: `docs/runbooks/news-ingestor-kr-scheduled-push.md`

- [ ] **Step 1: Add the broad-feed-only note to the news-ingestor runbook**

In `docs/runbooks/news-ingestor-kr-scheduled-push.md`, add a short section near the top:

```markdown
## Role (ROB-424)

`news-ingestor` is a **broad market-wide feed collector** populating `news_articles`.
It is NOT a report freshness gate or a citation/evidence source for investment
decisions. Symbol-level investment evidence comes exclusively from `get_news` /
`symbol_news_service` (on-demand). Production Prefect pause/unpause for
`news-ingestor` remains **operator-gated**; this PR does not change scheduler state.
Existing `news_articles` rows are preserved (no deletion/backfill).
```

- [ ] **Step 2: Commit docs**

```bash
git add docs/runbooks/news-ingestor-kr-scheduled-push.md
git commit -m "docs(ROB-424): document news-ingestor as broad-feed-only, not report evidence"
```

- [ ] **Step 3: get_news envelope regression (AC7)**

Run: `uv run pytest tests/mcp_server/tooling/test_get_news_envelope.py -v`
Expected: PASS (unchanged — get_news untouched).

- [ ] **Step 4: Full affected-suite + lint + types**

```bash
uv run pytest \
  tests/test_news_stage_on_demand.py \
  tests/analysis/stages/test_news_stage.py \
  tests/test_news_stage_fallback.py \
  tests/test_news_rss.py \
  tests/test_mcp_news_crypto_relevance.py \
  tests/test_market_news_briefing_formatter.py \
  tests/services/investment_dimensions/test_news_evidence.py \
  tests/services/investment_stages/test_hermes_context_news_dimension.py \
  tests/mcp_server/tooling/test_get_news_envelope.py -v
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ tests/   # or: make typecheck
```
Expected: all green. (CI lints `app/` AND `tests/` — do not skip the `tests/` scope.)

- [ ] **Step 5: Final grep guard (no residual decision-path use of broad surfaces)**

Run: `rg -n "get_news_articles_with_fallback|research_news_service|bulk_create_news_articles" app/analysis/`
Expected: no output (NewsStage no longer touches the broad-DB seam).

---

## Self-Review

**1. Spec coverage:**
- AC1 → Task 1 (`test_no_broad_db_seam_on_module`, on-demand-first rewrite). ✅
- AC2 → Task 1 (`ok`/`empty`/`error` status tests). ✅
- AC3 → Task 2 (`test_news_tool_names_exported`). ✅
- AC4 → Task 2 (`test_get_market_news_carries_legacy_surface_notice` + description). ✅
- AC5 → Task 3 (`data_health.source == "symbol_news"`). ✅
- AC6 → Task 4 (delete shim + grep guard). ✅
- AC7 → Task 5 Step 3 (envelope test). ✅
- AC8 (no new tool/scheduler/deletion/migration) → no tool added, no Prefect/alembic touched, no DELETE/backfill. ✅
- AC9 → Task 5 Step 4 (lint/types/tests). ✅

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". Deletions specified by exact file + `rg` identification. ✅

**3. Type consistency:** `fetch_symbol_news` → `SymbolNewsFetchResult` (`.status: str`, `.articles: list[SymbolNewsArticle]`); `SymbolNewsArticle.published_at` → `_SignalArticle.article_published_at`; `_compute_signals_from_articles` reads `.title`/`.article_published_at`/`.keywords` (all on `_SignalArticle`). `status` carried as a dict key (keeps `test_news_stage.py` mock valid). ✅

---

## Risks & Rollback
- **NewsStage verdict change** is behind `RESEARCH_PIPELINE_ENABLED=False` (default) → no live-traffic behavior change unless an operator enabled it. The change is the intended hardening.
- **`top_themes` becomes empty** for NewsStage (SymbolNewsArticle has no keywords) — already true for the prior KR on-demand path; not a regression.
- **`get_market_news` stays agent-callable** — only a docstring/response label keeps it out of decisions; acceptable because the actual decision path (NewsStage) is hardened and the snapshot report path is already on-demand.
- **Rollback:** `git revert` the PR. Migration 0, scheduler change 0, data deletion 0 — nothing stateful to undo.

## Related
- ROB-423 (#1106) — get_news citation persistence (precursor).
- ROB-398 — KR market-data collectors / Naver mapping.
