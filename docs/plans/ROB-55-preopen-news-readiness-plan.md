# ROB-55 — Preopen page: news readiness + latest news summary

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (this plan will be handed to a Sonnet implementer in the same session). Steps use checkbox (`- [ ]`) syntax for tracking.

**Linear:** https://linear.app/mgh3326/issue/ROB-55/auto-trader-preopen-page에-news-readiness와-latest-news-summary-연결

**Branch / worktree:** `feature/ROB-55-preopen-news-readiness` at `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-55-preopen-news-readiness`

**Goal:** Surface the existing news ingestion readiness + a small latest-news preview on the `/trading/decisions/preopen` page, so an operator can tell at a glance whether the news pipeline is fresh before acting on the preopen advisory.

**Architecture:**
- Backend: extend `PreopenLatestResponse` with a typed `news` field (readiness summary) and a `news_preview` list (≤5 latest articles for the market). Reuse the existing `get_news_readiness()` and add a thin `get_latest_news_preview()` helper. Service-side merge happens in `preopen_dashboard_service.get_latest_preopen_dashboard()`. The existing dict-form `source_freshness["news"]` and the `news_*` entries in `source_warnings` are preserved for back-compat.
- Frontend (Vite/React SPA): add a `NewsReadinessSection` rendered above the Candidates table. Status pill driven by a new `ReadinessStatusBadge` component (mirroring `StatusBadge` style). Stale and unavailable states are explicit, never silent.
- Read-only. No new LLM calls, no Hermes summarization, no NewsSignal extraction, no order/intent paths. The `forbidden imports` AST test in `tests/test_preopen_dashboard_service.py::test_no_forbidden_imports` keeps these guarantees.

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy 2.x async (backend), React 18 + TypeScript + Vite + Vitest + React Testing Library + MSW (frontend), CSS Modules (styling).

---

## Existing code to read first (do not skip)

- `app/routers/preopen.py` — route + auth dep + response model wiring.
- `app/schemas/preopen.py` — `PreopenLatestResponse` and nested models.
- `app/services/preopen_dashboard_service.py:154-195` — current `_merge_news_readiness()` (will be refactored to populate the new typed field, not removed).
- `app/services/llm_news_service.py`
  - `get_news_readiness()` (line 356) — readiness query.
  - `_news_readiness_payload()` (line 186) — readiness rules: `news_unavailable`, `news_run_unfinished`, `news_sources_empty`, `news_stale` (>180min default).
  - `get_news_articles()` (line 112) — opens its own `AsyncSessionLocal()`; we will NOT reuse this for the preview because the dashboard service already has a session.
- `app/models/news.py` — `NewsArticle` (id, title, url, source, feed_source, article_published_at, summary), `NewsIngestionRun` (run_uuid, market, source_counts, finished_at, status).
- `tests/test_preopen_dashboard_service.py` — pattern for mocking `get_latest_research_run`, `_linked_sessions`, and `get_news_readiness`.
- `tests/test_router_preopen.py` — pattern for auth + JSON shape assertions (uses an authenticated test client and a fake research run).
- `tests/test_news_ingestor_bulk.py:218-` — `test_preopen_dashboard_adds_news_stale_warning` is the closest precedent for stale tests.
- `frontend/trading-decision/src/pages/PreopenPage.tsx` — the page to extend.
- `frontend/trading-decision/src/api/types.ts:179-236` — `PreopenLatestResponse` TS type.
- `frontend/trading-decision/src/api/preopen.ts` — fetch wrapper (do not modify).
- `frontend/trading-decision/src/components/StatusBadge.tsx` + `.module.css` — copy pattern for the readiness badge.
- `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx` + `frontend/trading-decision/src/test/fixtures/preopen.ts` — extend with new fixtures and tests.
- `frontend/trading-decision/src/format/datetime.ts` — `formatDateTime` for ISO timestamps.

---

## File map

**Modify (backend):**
- `app/schemas/preopen.py` — add `NewsReadinessSummary`, `NewsArticlePreview`; add `news` and `news_preview` fields on `PreopenLatestResponse`; update `_FAIL_OPEN` defaults.
- `app/services/preopen_dashboard_service.py` — refactor `_merge_news_readiness()` to also return the typed summary; add `_load_news_preview()` and call sites; populate new fields on the response. Update `_FAIL_OPEN` to set `news=None, news_preview=[]`.
- `app/services/llm_news_service.py` — add `get_latest_news_preview()` helper that accepts an `AsyncSession` and a market/feed_set scope, returns up to N (default 5) recent `NewsArticle` rows mapped to `NewsArticlePreview`. No LLM calls, no summarization beyond passing through the stored `summary` column.

**Modify (frontend):**
- `frontend/trading-decision/src/api/types.ts` — add `PreopenNewsReadinessStatus`, `PreopenNewsReadinessSummary`, `PreopenNewsArticlePreview`; add `news` and `news_preview` on `PreopenLatestResponse`.
- `frontend/trading-decision/src/pages/PreopenPage.tsx` — render the new section.
- `frontend/trading-decision/src/pages/PreopenPage.module.css` — minimal layout for the section (no new color tokens; reuse existing palette).
- `frontend/trading-decision/src/test/fixtures/preopen.ts` — add fixture builders for ready/stale/unavailable variants and preview list.

**Create (frontend):**
- `frontend/trading-decision/src/components/ReadinessStatusBadge.tsx` — `<span>` with `ready | stale | unavailable` class.
- `frontend/trading-decision/src/components/ReadinessStatusBadge.module.css` — three states.
- `frontend/trading-decision/src/components/NewsReadinessSection.tsx` — section + preview list.
- `frontend/trading-decision/src/components/NewsReadinessSection.module.css` — section layout.

**Modify (tests):**
- `tests/test_preopen_dashboard_service.py` — new cases (ready / stale / unavailable / preview / preview-skipped-when-unavailable).
- `tests/test_router_preopen.py` — JSON shape assertions for `news` and `news_preview` keys.
- `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx` — new render cases.

**Do not touch:**
- KIS / Upbit / broker / order / intent / watch / token modules.
- Prefect schedulers, news-ingestor service code, the bulk ingest endpoint.
- `app/routers/preopen.py` (only the response model needs no change since we extend `PreopenLatestResponse` in place).

---

## API contract (additions)

`GET /trading/api/preopen/latest?market_scope=kr` (auth required, unchanged).

New top-level fields on `PreopenLatestResponse`:

```jsonc
{
  // ...existing fields...
  "news": {
    "status": "ready" | "stale" | "unavailable",
    "is_ready": true,
    "is_stale": false,
    "latest_run_uuid": "uuid|null",
    "latest_status": "success" | "partial" | null,
    "latest_finished_at": "2026-04-30T05:30:00+09:00|null",
    "latest_article_published_at": "2026-04-30T05:25:11+09:00|null",
    "source_counts": { "browser_naver_mainnews": 20, "yna_market": 12 },
    "warnings": ["news_stale"],
    "max_age_minutes": 180
  } | null,
  "news_preview": [
    {
      "id": 12345,
      "title": "삼성전자 1분기 실적 ...",
      "url": "https://...",
      "source": "매일경제",
      "feed_source": "mk_stock",
      "published_at": "2026-04-30T05:00:00+09:00|null",
      "summary": "..." | null
    }
  ]
}
```

Rules:
- `news.status` is derived in service: `unavailable` if `latest_run is None` OR `news_unavailable` in warnings; else `stale` if `is_stale` OR `news_stale` in warnings; else `ready`. Source of truth is the existing `_news_readiness_payload()` output — no parallel rule logic.
- `news = null` only when readiness lookup itself raised (degraded fallback). In that case `source_warnings` still contains `news_readiness_unavailable` (current behavior preserved).
- `news_preview` is an empty list (never null), capped at 5 rows. When `news.status == "unavailable"` and there is no usable feed_source set, return `[]` rather than failing.
- `source_freshness["news"]` and the existing news warning entries in `source_warnings` are kept untouched. The new typed `news` field is the recommended consumer; the dict-form is back-compat only.

---

## Backend tasks

### Task 1: Add typed schema for news readiness + preview

**Files:**
- Modify: `app/schemas/preopen.py`

- [ ] **Step 1: Add the new models and extend `PreopenLatestResponse`**

```python
# app/schemas/preopen.py

from typing import Any, Literal

# ... existing imports ...

NewsReadinessStatus = Literal["ready", "stale", "unavailable"]


class NewsArticlePreview(BaseModel):
    id: int
    title: str
    url: str
    source: str | None
    feed_source: str | None
    published_at: datetime | None
    summary: str | None


class NewsReadinessSummary(BaseModel):
    status: NewsReadinessStatus
    is_ready: bool
    is_stale: bool
    latest_run_uuid: str | None
    latest_status: str | None
    latest_finished_at: datetime | None
    latest_article_published_at: datetime | None
    source_counts: dict[str, int]
    warnings: list[str]
    max_age_minutes: int


class PreopenLatestResponse(BaseModel):
    # ... all existing fields unchanged ...
    news: NewsReadinessSummary | None = None
    news_preview: list[NewsArticlePreview] = []
```

- [ ] **Step 2: Run schema import sanity check**

```bash
uv run python -c "from app.schemas.preopen import PreopenLatestResponse, NewsReadinessSummary, NewsArticlePreview; print('ok')"
```

Expected: `ok`. No traceback.

- [ ] **Step 3: Commit**

```bash
git add app/schemas/preopen.py
git commit -m "feat(ROB-55): add news readiness + preview to PreopenLatestResponse schema"
```

---

### Task 2: Add `get_latest_news_preview()` helper in llm_news_service

**Files:**
- Modify: `app/services/llm_news_service.py`

- [ ] **Step 1: Write the failing unit test**

Create / extend `tests/test_llm_news_preview.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_latest_news_preview_returns_mapped_rows():
    from app.schemas.preopen import NewsArticlePreview
    from app.services.llm_news_service import get_latest_news_preview

    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            id=1,
            title="hello",
            url="https://example.com/a",
            source="MK",
            feed_source="mk_stock",
            article_published_at=now,
            summary="s",
        ),
        SimpleNamespace(
            id=2,
            title="world",
            url="https://example.com/b",
            source=None,
            feed_source="yna_market",
            article_published_at=now - timedelta(minutes=5),
            summary=None,
        ),
    ]
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=result)

    out = await get_latest_news_preview(
        db=db, feed_sources=["mk_stock", "yna_market"], limit=5
    )

    assert all(isinstance(item, NewsArticlePreview) for item in out)
    assert [item.id for item in out] == [1, 2]
    assert out[0].published_at is not None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_latest_news_preview_empty_when_no_feed_sources():
    from app.services.llm_news_service import get_latest_news_preview

    db = AsyncMock()
    out = await get_latest_news_preview(db=db, feed_sources=[], limit=5)
    assert out == []
    db.execute.assert_not_awaited()
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
uv run pytest tests/test_llm_news_preview.py -v
```

Expected: `ImportError` for `get_latest_news_preview` (function does not exist yet).

- [ ] **Step 3: Implement `get_latest_news_preview()`**

In `app/services/llm_news_service.py`, add (near `get_news_readiness`):

```python
from app.schemas.preopen import NewsArticlePreview


async def get_latest_news_preview(
    *,
    db: AsyncSession,
    feed_sources: list[str],
    limit: int = 5,
) -> list[NewsArticlePreview]:
    """Return the N most recent news articles for the given feed sources.

    Read-only, no LLM. The caller is expected to derive `feed_sources`
    from the latest ingestion run's `source_counts.keys()`.
    """
    if not feed_sources or limit <= 0:
        return []

    stmt = (
        select(NewsArticle)
        .where(NewsArticle.feed_source.in_(feed_sources))
        .order_by(NewsArticle.article_published_at.desc().nulls_last())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        NewsArticlePreview(
            id=row.id,
            title=row.title,
            url=row.url,
            source=row.source,
            feed_source=row.feed_source,
            published_at=row.article_published_at,
            summary=row.summary,
        )
        for row in rows
    ]
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
uv run pytest tests/test_llm_news_preview.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Run forbidden-imports guard to make sure llm_news_service is still safe**

```bash
uv run pytest tests/test_preopen_dashboard_service.py::test_no_forbidden_imports -v
```

Expected: pass. (We did not introduce any new imports in `app/services/preopen_dashboard_service.py` yet.)

- [ ] **Step 6: Commit**

```bash
git add app/services/llm_news_service.py tests/test_llm_news_preview.py
git commit -m "feat(ROB-55): add get_latest_news_preview helper for preopen"
```

---

### Task 3: Wire news readiness summary + preview into preopen dashboard service

**Files:**
- Modify: `app/services/preopen_dashboard_service.py`

- [ ] **Step 1: Write failing tests for the new fields**

Append to `tests/test_preopen_dashboard_service.py` (after the existing tests, before `test_no_forbidden_imports`):

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_ready_and_preview_attached():
    from app.schemas.preopen import NewsArticlePreview
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=True,
        is_stale=False,
        warnings=[],
        source_counts={"browser_naver_mainnews": 20, "yna_market": 12},
    )
    preview = [
        NewsArticlePreview(
            id=1,
            title="t",
            url="u",
            source="MK",
            feed_source="mk_stock",
            published_at=datetime.now(UTC),
            summary=None,
        )
    ]

    with (
        patch.object(
            research_run_service,
            "get_latest_research_run",
            new=AsyncMock(return_value=run),
        ),
        patch.object(
            preopen_dashboard_service,
            "_linked_sessions",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_news_readiness",
            new=AsyncMock(return_value=readiness),
        ),
        patch.object(
            preopen_dashboard_service,
            "get_latest_news_preview",
            new=AsyncMock(return_value=preview),
        ),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(), user_id=7, market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "ready"
    assert result.news.source_counts["browser_naver_mainnews"] == 20
    assert len(result.news_preview) == 1
    assert result.news_preview[0].title == "t"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_stale_status_when_warning_present():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=True,
        warnings=["news_stale"],
        source_counts={"browser_naver_mainnews": 20},
    )

    with (
        patch.object(research_run_service, "get_latest_research_run",
                     new=AsyncMock(return_value=run)),
        patch.object(preopen_dashboard_service, "_linked_sessions",
                     new=AsyncMock(return_value=[])),
        patch.object(preopen_dashboard_service, "get_news_readiness",
                     new=AsyncMock(return_value=readiness)),
        patch.object(preopen_dashboard_service, "get_latest_news_preview",
                     new=AsyncMock(return_value=[])),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(), user_id=7, market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "stale"
    assert "news_stale" in result.news.warnings


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_unavailable_when_no_run():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()
    readiness = _make_news_readiness(
        is_ready=False,
        is_stale=True,
        latest_run_uuid=None,
        latest_status=None,
        latest_finished_at=None,
        warnings=["news_unavailable", "news_stale"],
        source_counts={},
    )

    with (
        patch.object(research_run_service, "get_latest_research_run",
                     new=AsyncMock(return_value=run)),
        patch.object(preopen_dashboard_service, "_linked_sessions",
                     new=AsyncMock(return_value=[])),
        patch.object(preopen_dashboard_service, "get_news_readiness",
                     new=AsyncMock(return_value=readiness)),
        patch.object(preopen_dashboard_service, "get_latest_news_preview",
                     new=AsyncMock(return_value=[])),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(), user_id=7, market_scope="kr",
        )

    assert result.news is not None
    assert result.news.status == "unavailable"
    assert result.news_preview == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_news_summary_none_when_readiness_lookup_raises():
    from app.services import preopen_dashboard_service, research_run_service

    run = _make_run()

    with (
        patch.object(research_run_service, "get_latest_research_run",
                     new=AsyncMock(return_value=run)),
        patch.object(preopen_dashboard_service, "_linked_sessions",
                     new=AsyncMock(return_value=[])),
        patch.object(preopen_dashboard_service, "get_news_readiness",
                     new=AsyncMock(side_effect=RuntimeError("redis down"))),
        patch.object(preopen_dashboard_service, "get_latest_news_preview",
                     new=AsyncMock(return_value=[])),
    ):
        result = await preopen_dashboard_service.get_latest_preopen_dashboard(
            db=AsyncMock(), user_id=7, market_scope="kr",
        )

    assert result.news is None
    assert result.news_preview == []
    assert "news_readiness_unavailable" in result.source_warnings
```

Add the import at top of test file if missing:
```python
from datetime import UTC, datetime
```

- [ ] **Step 2: Run the new tests to confirm they fail**

```bash
uv run pytest tests/test_preopen_dashboard_service.py -v -k "news_summary"
```

Expected: 4 failed (assertion failures on `result.news`, which is missing).

- [ ] **Step 3: Implement the typed merge + preview wiring**

Edit `app/services/preopen_dashboard_service.py`:

1. Replace the `from app.services.llm_news_service import get_news_readiness` line with:

```python
from app.services.llm_news_service import (
    get_latest_news_preview,
    get_news_readiness,
)
```

2. Update imports at top:

```python
from app.schemas.preopen import (
    CandidateSummary,
    LinkedSessionRef,
    NewsArticlePreview,
    NewsReadinessSummary,
    PreopenLatestResponse,
    ReconciliationSummary,
)
```

3. Update `_FAIL_OPEN`:

```python
_FAIL_OPEN = PreopenLatestResponse(
    # ... existing fields ...
    news=None,
    news_preview=[],
)
```

4. Replace `_merge_news_readiness()` body and add a derivation helper:

```python
def _derive_news_status(readiness) -> str:
    warnings = list(readiness.warnings or [])
    if "news_unavailable" in warnings or readiness.latest_run_uuid is None:
        return "unavailable"
    if readiness.is_stale or "news_stale" in warnings:
        return "stale"
    if readiness.is_ready:
        return "ready"
    # Defensive: not ready but not flagged stale → treat as stale, not silent.
    return "stale"


async def _build_news_section(
    db: AsyncSession,
    *,
    market_scope: str,
    source_freshness: dict | None,
    source_warnings: list[str],
) -> tuple[
    NewsReadinessSummary | None,
    list[NewsArticlePreview],
    dict | None,
    list[str],
]:
    """Fetch readiness + latest preview, return both typed and merged-dict views."""
    try:
        readiness = await get_news_readiness(market=market_scope, db=db)
    except Exception:
        logger.warning(
            "Failed to look up news readiness for preopen dashboard",
            exc_info=True,
            extra={"market_scope": market_scope},
        )
        merged_warnings = list(source_warnings)
        if "news_readiness_unavailable" not in merged_warnings:
            merged_warnings.append("news_readiness_unavailable")
        return None, [], source_freshness, merged_warnings

    merged_freshness = dict(source_freshness or {})
    merged_freshness["news"] = {
        "is_ready": readiness.is_ready,
        "is_stale": readiness.is_stale,
        "latest_run_uuid": readiness.latest_run_uuid,
        "latest_status": readiness.latest_status,
        "latest_finished_at": readiness.latest_finished_at.isoformat()
        if readiness.latest_finished_at
        else None,
        "latest_article_published_at": readiness.latest_article_published_at.isoformat()
        if readiness.latest_article_published_at
        else None,
        "source_counts": readiness.source_counts,
        "warnings": readiness.warnings,
        "max_age_minutes": readiness.max_age_minutes,
    }
    merged_warnings = list(source_warnings)
    for warning in readiness.warnings:
        if warning not in merged_warnings:
            merged_warnings.append(warning)

    summary = NewsReadinessSummary(
        status=_derive_news_status(readiness),
        is_ready=readiness.is_ready,
        is_stale=readiness.is_stale,
        latest_run_uuid=str(readiness.latest_run_uuid) if readiness.latest_run_uuid else None,
        latest_status=readiness.latest_status,
        latest_finished_at=readiness.latest_finished_at,
        latest_article_published_at=readiness.latest_article_published_at,
        source_counts=dict(readiness.source_counts or {}),
        warnings=list(readiness.warnings or []),
        max_age_minutes=readiness.max_age_minutes,
    )

    feed_sources = list((readiness.source_counts or {}).keys())
    try:
        preview = await get_latest_news_preview(
            db=db, feed_sources=feed_sources, limit=5
        )
    except Exception:
        logger.warning(
            "Failed to load news preview for preopen dashboard",
            exc_info=True,
            extra={"market_scope": market_scope},
        )
        preview = []

    return summary, preview, merged_freshness, merged_warnings
```

5. In `get_latest_preopen_dashboard()`, replace the `_merge_news_readiness(...)` call with:

```python
news_summary, news_preview, source_freshness, source_warnings = await _build_news_section(
    db,
    market_scope=market_scope,
    source_freshness=run.source_freshness,
    source_warnings=list(run.source_warnings),
)
```

6. Pass the new fields into the `PreopenLatestResponse(...)` constructor at the end:

```python
return PreopenLatestResponse(
    # ... existing fields ...
    news=news_summary,
    news_preview=news_preview,
)
```

7. Delete the old `_merge_news_readiness()` function (replaced by `_build_news_section()`).

- [ ] **Step 4: Run all preopen service tests**

```bash
uv run pytest tests/test_preopen_dashboard_service.py -v
```

Expected: all pass, including the new `test_news_summary_*` cases.

- [ ] **Step 5: Run the existing news/stale integration test**

```bash
uv run pytest tests/test_news_ingestor_bulk.py::TestNewsReadinessPreopenIntegration -v
```

Expected: pass (the existing `test_preopen_dashboard_adds_news_stale_warning` still works because `source_freshness["news"]` and `source_warnings` are preserved).

- [ ] **Step 6: Commit**

```bash
git add app/services/preopen_dashboard_service.py tests/test_preopen_dashboard_service.py
git commit -m "feat(ROB-55): wire news readiness summary + preview into preopen dashboard"
```

---

### Task 4: Router-level JSON shape assertion

**Files:**
- Modify: `tests/test_router_preopen.py`

- [ ] **Step 1: Read the existing test file to find the "with run returns full payload" test**

```bash
uv run pytest tests/test_router_preopen.py -v --collect-only
```

Confirm the existing test names. The target is `test_get_latest_preopen_with_run_returns_full_payload` (around line 149).

- [ ] **Step 2: Add new assertions for `news` and `news_preview` keys**

In the existing `test_get_latest_preopen_with_run_returns_full_payload` test (or add a sibling test), assert:

```python
body = response.json()
assert "news" in body
assert "news_preview" in body
assert isinstance(body["news_preview"], list)
# When the test fixture stubs readiness as ready:
if body["news"] is not None:
    assert body["news"]["status"] in {"ready", "stale", "unavailable"}
    assert "source_counts" in body["news"]
```

If the existing test patches `get_latest_preopen_dashboard` (it likely returns a fully-built schema), update the test to construct a `PreopenLatestResponse` that includes a non-None `news` and a 1-item `news_preview`. Otherwise add a sibling test:

```python
@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_latest_preopen_returns_news_section(authed_client, monkeypatch):
    from app.routers import preopen as preopen_router
    from app.schemas.preopen import (
        NewsArticlePreview,
        NewsReadinessSummary,
        PreopenLatestResponse,
    )

    response_obj = PreopenLatestResponse(
        has_run=True,
        advisory_used=False,
        advisory_skipped_reason=None,
        run_uuid=uuid4(),
        market_scope="kr",
        stage="preopen",
        status="open",
        strategy_name="x",
        source_profile="roadmap",
        generated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
        notes=None,
        market_brief=None,
        source_freshness=None,
        source_warnings=[],
        advisory_links=[],
        candidate_count=0,
        reconciliation_count=0,
        candidates=[],
        reconciliations=[],
        linked_sessions=[],
        news=NewsReadinessSummary(
            status="ready",
            is_ready=True,
            is_stale=False,
            latest_run_uuid="abc",
            latest_status="success",
            latest_finished_at=datetime.now(UTC),
            latest_article_published_at=datetime.now(UTC),
            source_counts={"mk_stock": 10},
            warnings=[],
            max_age_minutes=180,
        ),
        news_preview=[
            NewsArticlePreview(
                id=1, title="t", url="u",
                source="MK", feed_source="mk_stock",
                published_at=datetime.now(UTC), summary=None,
            )
        ],
    )

    async def fake(*args, **kwargs):
        return response_obj

    monkeypatch.setattr(
        preopen_router.preopen_dashboard_service,
        "get_latest_preopen_dashboard",
        fake,
    )

    r = await authed_client.get("/trading/api/preopen/latest?market_scope=kr")
    assert r.status_code == 200
    body = r.json()
    assert body["news"]["status"] == "ready"
    assert body["news_preview"][0]["title"] == "t"
```

(Use whatever `authed_client` / monkeypatch fixture pattern the existing tests use — copy from `test_get_latest_preopen_with_run_returns_full_payload`. Do not introduce a new auth pattern.)

- [ ] **Step 3: Run the router tests**

```bash
uv run pytest tests/test_router_preopen.py -v
```

Expected: all pass, including auth-401 and 422 cases (unchanged).

- [ ] **Step 4: Commit**

```bash
git add tests/test_router_preopen.py
git commit -m "test(ROB-55): assert news + news_preview keys in /preopen/latest"
```

---

## Frontend tasks

### Task 5: TypeScript types

**Files:**
- Modify: `frontend/trading-decision/src/api/types.ts`

- [ ] **Step 1: Add types**

Append (just below the existing `PreopenLatestResponse` block):

```ts
export type PreopenNewsReadinessStatus = "ready" | "stale" | "unavailable";

export interface PreopenNewsReadinessSummary {
  status: PreopenNewsReadinessStatus;
  is_ready: boolean;
  is_stale: boolean;
  latest_run_uuid: string | null;
  latest_status: string | null;
  latest_finished_at: IsoDateTime | null;
  latest_article_published_at: IsoDateTime | null;
  source_counts: Record<string, number>;
  warnings: string[];
  max_age_minutes: number;
}

export interface PreopenNewsArticlePreview {
  id: number;
  title: string;
  url: string;
  source: string | null;
  feed_source: string | null;
  published_at: IsoDateTime | null;
  summary: string | null;
}
```

And extend `PreopenLatestResponse`:

```ts
export interface PreopenLatestResponse {
  // ... all existing fields ...
  news: PreopenNewsReadinessSummary | null;
  news_preview: PreopenNewsArticlePreview[];
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend/trading-decision && npm run typecheck
```

(Run whatever script the project uses for type checking — see `frontend/trading-decision/package.json`. If `typecheck` is not defined, use `npx tsc --noEmit`.) Expected: no errors. Existing usages of `PreopenLatestResponse` will fail in fixtures next, which is fine — fix in the next task.

- [ ] **Step 3: Commit (after fixtures are updated, see Task 6 — bundle these two commits if you prefer)**

---

### Task 6: Update fixtures

**Files:**
- Modify: `frontend/trading-decision/src/test/fixtures/preopen.ts`

- [ ] **Step 1: Add new fixture builders**

```ts
import type {
  PreopenCandidateSummary,
  PreopenLatestResponse,
  PreopenLinkedSession,
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
  PreopenReconciliationSummary,
} from "../../api/types";

// ... existing builders ...

export function makePreopenNewsReady(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    status: "ready",
    is_ready: true,
    is_stale: false,
    latest_run_uuid: "news-run-1",
    latest_status: "success",
    latest_finished_at: now,
    latest_article_published_at: now,
    source_counts: { mk_stock: 12, yna_market: 8 },
    warnings: [],
    max_age_minutes: 180,
    ...overrides,
  };
}

export function makePreopenNewsStale(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    ...makePreopenNewsReady(),
    status: "stale",
    is_ready: false,
    is_stale: true,
    warnings: ["news_stale"],
    ...overrides,
  };
}

export function makePreopenNewsUnavailable(
  overrides: Partial<PreopenNewsReadinessSummary> = {},
): PreopenNewsReadinessSummary {
  return {
    status: "unavailable",
    is_ready: false,
    is_stale: true,
    latest_run_uuid: null,
    latest_status: null,
    latest_finished_at: null,
    latest_article_published_at: null,
    source_counts: {},
    warnings: ["news_unavailable", "news_stale"],
    max_age_minutes: 180,
    ...overrides,
  };
}

export function makePreopenNewsArticle(
  overrides: Partial<PreopenNewsArticlePreview> = {},
): PreopenNewsArticlePreview {
  return {
    id: 1001,
    title: "삼성전자 1분기 실적 발표",
    url: "https://example.com/article/1001",
    source: "MK",
    feed_source: "mk_stock",
    published_at: now,
    summary: null,
    ...overrides,
  };
}
```

- [ ] **Step 2: Update existing builders to populate new fields**

In `makePreopenResponse(...)`:

```ts
{
  // ... existing fields ...
  news: makePreopenNewsReady(),
  news_preview: [makePreopenNewsArticle()],
  ...overrides,
}
```

In `makePreopenFailOpen(...)`:

```ts
{
  // ... existing fields ...
  news: null,
  news_preview: [],
  ...overrides,
}
```

- [ ] **Step 3: Type-check**

```bash
cd frontend/trading-decision && npx tsc --noEmit
```

Expected: no type errors.

- [ ] **Step 4: Commit (Tasks 5 + 6 together)**

```bash
git add frontend/trading-decision/src/api/types.ts \
        frontend/trading-decision/src/test/fixtures/preopen.ts
git commit -m "feat(ROB-55): extend preopen TS types + fixtures with news readiness"
```

---

### Task 7: ReadinessStatusBadge component

**Files:**
- Create: `frontend/trading-decision/src/components/ReadinessStatusBadge.tsx`
- Create: `frontend/trading-decision/src/components/ReadinessStatusBadge.module.css`

- [ ] **Step 1: Implement the component**

`ReadinessStatusBadge.tsx`:

```tsx
import type { PreopenNewsReadinessStatus } from "../api/types";
import styles from "./ReadinessStatusBadge.module.css";

const LABELS: Record<PreopenNewsReadinessStatus, string> = {
  ready: "Ready",
  stale: "Stale",
  unavailable: "Unavailable",
};

export interface ReadinessStatusBadgeProps {
  status: PreopenNewsReadinessStatus;
}

export default function ReadinessStatusBadge({
  status,
}: ReadinessStatusBadgeProps) {
  return (
    <span
      className={`${styles.badge} ${styles[status]}`}
      data-status={status}
      role="status"
    >
      {LABELS[status]}
    </span>
  );
}
```

`ReadinessStatusBadge.module.css`:

```css
.badge {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 600;
  border: 1px solid transparent;
}

.ready {
  background: #e6f4ea;
  color: #137333;
  border-color: #cdebd6;
}

.stale {
  background: #fff4e5;
  color: #a65d00;
  border-color: #f6dcb1;
}

.unavailable {
  background: #fdecec;
  color: #b3261e;
  border-color: #f4c1bd;
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend/trading-decision && npx tsc --noEmit
```

Expected: clean.

---

### Task 8: NewsReadinessSection component

**Files:**
- Create: `frontend/trading-decision/src/components/NewsReadinessSection.tsx`
- Create: `frontend/trading-decision/src/components/NewsReadinessSection.module.css`

- [ ] **Step 1: Implement component**

`NewsReadinessSection.tsx`:

```tsx
import type {
  PreopenNewsArticlePreview,
  PreopenNewsReadinessSummary,
} from "../api/types";
import { formatDateTime } from "../format/datetime";
import ReadinessStatusBadge from "./ReadinessStatusBadge";
import styles from "./NewsReadinessSection.module.css";

export interface NewsReadinessSectionProps {
  news: PreopenNewsReadinessSummary | null;
  preview: PreopenNewsArticlePreview[];
}

export default function NewsReadinessSection({
  news,
  preview,
}: NewsReadinessSectionProps) {
  if (news === null) {
    return (
      <section
        aria-label="News readiness"
        className={styles.section}
        data-testid="news-readiness-section"
      >
        <header className={styles.header}>
          <h2>News readiness</h2>
          <ReadinessStatusBadge status="unavailable" />
        </header>
        <p className={styles.muted}>
          News readiness lookup failed. Treat this preopen as if news is
          unavailable.
        </p>
      </section>
    );
  }

  const sourceEntries = Object.entries(news.source_counts);

  return (
    <section
      aria-label="News readiness"
      className={styles.section}
      data-testid="news-readiness-section"
    >
      <header className={styles.header}>
        <h2>News readiness</h2>
        <ReadinessStatusBadge status={news.status} />
      </header>

      <dl className={styles.meta}>
        <div>
          <dt>Latest run</dt>
          <dd>{formatDateTime(news.latest_finished_at)}</dd>
        </div>
        <div>
          <dt>Latest article</dt>
          <dd>{formatDateTime(news.latest_article_published_at)}</dd>
        </div>
        <div>
          <dt>Freshness window</dt>
          <dd>{news.max_age_minutes} min</dd>
        </div>
      </dl>

      {news.status !== "ready" ? (
        <p className={styles.warningLine} role="status">
          {news.status === "stale"
            ? `News is older than ${news.max_age_minutes} min — verify before acting.`
            : "News pipeline did not report a recent successful run."}
        </p>
      ) : null}

      {sourceEntries.length > 0 ? (
        <ul aria-label="News source counts" className={styles.sourceList}>
          {sourceEntries.map(([source, count]) => (
            <li className={styles.sourceChip} key={source}>
              {source}: {count}
            </li>
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>No source counts available.</p>
      )}

      <h3 className={styles.previewHeading}>
        Latest articles ({preview.length})
      </h3>
      {preview.length === 0 ? (
        <p className={styles.muted}>No recent articles to preview.</p>
      ) : (
        <ul className={styles.previewList}>
          {preview.map((item) => (
            <li className={styles.previewItem} key={item.id}>
              <a href={item.url} rel="noreferrer noopener" target="_blank">
                {item.title}
              </a>
              <span className={styles.previewMeta}>
                {item.source ?? item.feed_source ?? "—"} ·{" "}
                {formatDateTime(item.published_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
```

`NewsReadinessSection.module.css`:

```css
.section {
  border: 1px solid #e2e6ec;
  background: #ffffff;
  border-radius: 8px;
  padding: 1rem 1.25rem;
  margin-bottom: 1.25rem;
}

.header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}

.header h2 {
  margin: 0;
  font-size: 1.1rem;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 1.25rem;
  margin: 0.5rem 0 0.75rem;
}

.meta dt {
  font-size: 0.75rem;
  color: #5f6b7a;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.meta dd {
  margin: 0;
  font-size: 0.9rem;
  color: #172033;
}

.warningLine {
  background: #fff4e5;
  color: #a65d00;
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  margin: 0.5rem 0 0.75rem;
}

.sourceList {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  list-style: none;
  padding: 0;
  margin: 0 0 0.75rem;
}

.sourceChip {
  background: #f1f5f9;
  color: #1f5f99;
  border-radius: 999px;
  padding: 0.15rem 0.6rem;
  font-size: 0.8rem;
}

.previewHeading {
  font-size: 0.95rem;
  margin: 0.5rem 0 0.4rem;
}

.previewList {
  list-style: none;
  padding: 0;
  margin: 0;
}

.previewItem {
  border-top: 1px solid #f0f3f7;
  padding: 0.5rem 0;
}

.previewItem:first-child {
  border-top: none;
}

.previewItem a {
  color: #1f5f99;
  text-decoration: none;
  font-weight: 500;
}

.previewItem a:hover {
  text-decoration: underline;
}

.previewMeta {
  display: block;
  color: #5f6b7a;
  font-size: 0.78rem;
  margin-top: 0.1rem;
}

.muted {
  color: #5f6b7a;
  margin: 0.25rem 0;
}
```

- [ ] **Step 2: Type-check**

```bash
cd frontend/trading-decision && npx tsc --noEmit
```

Expected: clean.

- [ ] **Step 3: Commit Tasks 7 + 8**

```bash
git add frontend/trading-decision/src/components/ReadinessStatusBadge.tsx \
        frontend/trading-decision/src/components/ReadinessStatusBadge.module.css \
        frontend/trading-decision/src/components/NewsReadinessSection.tsx \
        frontend/trading-decision/src/components/NewsReadinessSection.module.css
git commit -m "feat(ROB-55): add NewsReadinessSection + ReadinessStatusBadge"
```

---

### Task 9: Render the section on PreopenPage

**Files:**
- Modify: `frontend/trading-decision/src/pages/PreopenPage.tsx`

- [ ] **Step 1: Add imports**

```tsx
import NewsReadinessSection from "../components/NewsReadinessSection";
```

- [ ] **Step 2: Render the section above the Candidates table**

In the `has_run` branch, just before `{data.candidates.length > 0 ? (` and AFTER the source warnings list (so the section is visually below the warnings), insert:

```tsx
<NewsReadinessSection news={data.news} preview={data.news_preview} />
```

For the `!data.has_run` branch (fail-open banner), DO NOT render the section — keep the page minimal as it is today. The news status will be `null` in fail-open responses and would be redundant alongside the "no run" banner.

- [ ] **Step 3: Manual smoke (optional but recommended)**

```bash
cd frontend/trading-decision && npm run dev
```

Open `/trading/decisions/preopen` against a local backend with a seeded `news_ingestion_runs` row. Verify the section renders. (Skip if no local DB; the Vitest assertions in the next task are the authoritative check.)

- [ ] **Step 4: Commit**

```bash
git add frontend/trading-decision/src/pages/PreopenPage.tsx
git commit -m "feat(ROB-55): render NewsReadinessSection on preopen page"
```

---

### Task 10: Frontend tests

**Files:**
- Modify: `frontend/trading-decision/src/__tests__/PreopenPage.test.tsx`

- [ ] **Step 1: Add new tests**

```tsx
import {
  makePreopenFailOpen,
  makePreopenLinkedSession,
  makePreopenNewsArticle,
  makePreopenNewsStale,
  makePreopenNewsUnavailable,
  makePreopenResponse,
} from "../test/fixtures/preopen";

// ... existing tests ...

it("renders Ready badge with source counts and a news preview link", async () => {
  mockFetch({
    [PREOPEN_URL]: () =>
      new Response(
        JSON.stringify(
          makePreopenResponse({
            news_preview: [
              makePreopenNewsArticle({
                id: 9001,
                title: "삼성전자 영업이익",
                url: "https://example.com/9001",
              }),
            ],
          }),
        ),
      ),
  });

  render(<PreopenPage />, { wrapper: MemoryRouter });

  expect(await screen.findByTestId("news-readiness-section")).toBeInTheDocument();
  expect(screen.getByText("Ready")).toBeInTheDocument();
  expect(screen.getByText(/mk_stock: 12/)).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: /삼성전자 영업이익/ }),
  ).toHaveAttribute("href", "https://example.com/9001");
});

it("renders Stale badge with explicit warning text", async () => {
  mockFetch({
    [PREOPEN_URL]: () =>
      new Response(
        JSON.stringify(
          makePreopenResponse({
            news: makePreopenNewsStale(),
          }),
        ),
      ),
  });

  render(<PreopenPage />, { wrapper: MemoryRouter });

  expect(await screen.findByText("Stale")).toBeInTheDocument();
  expect(
    screen.getByText(/News is older than 180 min/i),
  ).toBeInTheDocument();
});

it("renders Unavailable badge when news section reports no data", async () => {
  mockFetch({
    [PREOPEN_URL]: () =>
      new Response(
        JSON.stringify(
          makePreopenResponse({
            news: makePreopenNewsUnavailable(),
            news_preview: [],
          }),
        ),
      ),
  });

  render(<PreopenPage />, { wrapper: MemoryRouter });

  expect(await screen.findByText("Unavailable")).toBeInTheDocument();
  expect(
    screen.getByText(/No recent articles to preview/i),
  ).toBeInTheDocument();
});

it("renders Unavailable badge with degraded message when news is null", async () => {
  mockFetch({
    [PREOPEN_URL]: () =>
      new Response(
        JSON.stringify(
          makePreopenResponse({
            news: null,
            news_preview: [],
          }),
        ),
      ),
  });

  render(<PreopenPage />, { wrapper: MemoryRouter });

  expect(await screen.findByText("Unavailable")).toBeInTheDocument();
  expect(
    screen.getByText(/News readiness lookup failed/i),
  ).toBeInTheDocument();
});
```

- [ ] **Step 2: Run frontend tests**

```bash
cd frontend/trading-decision && npm test -- --run
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add frontend/trading-decision/src/__tests__/PreopenPage.test.tsx
git commit -m "test(ROB-55): cover news readiness ready/stale/unavailable states"
```

---

## Verification

- [ ] **Step 1: Backend full test suite (touched modules)**

```bash
uv run pytest tests/test_preopen_dashboard_service.py \
              tests/test_router_preopen.py \
              tests/test_news_ingestor_bulk.py \
              tests/test_news_ingestor_ingest_token_auth.py \
              tests/test_llm_news_preview.py -v
```

Expected: all pass. The bulk ingest token auth and existing news_stale tests must continue to pass — proves we did not break adjacent flows.

- [ ] **Step 2: Backend lint + typecheck**

```bash
make lint
```

Expected: clean. (If a new ruff rule trips on the new function, fix in place; do not silence.)

- [ ] **Step 3: Frontend test + typecheck**

```bash
cd frontend/trading-decision && npm test -- --run && npx tsc --noEmit
```

Expected: all pass, no TS errors.

- [ ] **Step 4: Forbidden imports invariant**

```bash
uv run pytest tests/test_preopen_dashboard_service.py::test_no_forbidden_imports -v
```

Expected: pass. We only added an import for `get_latest_news_preview` from `app.services.llm_news_service`, which the test already permits.

- [ ] **Step 5: Auth invariant**

```bash
uv run pytest tests/test_router_preopen.py::test_get_latest_preopen_unauthenticated_401 -v
```

Expected: pass. Confirms unauthenticated 401 is unchanged.

- [ ] **Step 6: Push branch and open PR (do NOT merge)**

```bash
git push -u origin feature/ROB-55-preopen-news-readiness
gh pr create --base main \
  --title "feat: surface news readiness + latest news preview on preopen page (ROB-55)" \
  --body "..."
```

PR body should include:
- Summary: surface readiness and a small preview on `/trading/decisions/preopen`.
- Out of scope (explicit): no NewsSignal extraction, no LLM summarization, no order/intent paths, no scheduler changes, no token rotation.
- Test plan checklist (mirror Verification steps).

---

## Risks & non-goals

**Risks**

- *Latest article query is unbounded by market.* `NewsArticle` does not have a market column; we filter by `feed_source ∈ source_counts.keys()` from the latest run. If a market's run reports no source counts, the preview is empty (intentional). Do not relax this filter to "all feeds" — it would leak cross-market noise.
- *Datetime normalization.* `_news_readiness_payload()` calls `to_kst_naive()`, so `latest_finished_at` and `latest_article_published_at` are tz-naive KST. Pydantic will serialize them as naive ISO timestamps. Frontend `formatDateTime` already accepts that shape. Do not re-attach UTC.
- *`source_freshness` dict-form is preserved.* Don't be tempted to drop it — `tests/test_news_ingestor_bulk.py::test_preopen_dashboard_adds_news_stale_warning` reads `source_freshness["news"]`.
- *`news.latest_run_uuid` is currently a stringified UUID via `_news_readiness_payload`.* The summary type is `str | None` accordingly. If readiness ever moves to a real `UUID`, both schema and frontend must be updated.

**Explicit non-goals (do NOT do)**

- Do NOT implement NewsSignal extraction.
- Do NOT add LLM/Hermes summarization. The `summary` field comes from the existing column only.
- Do NOT change TradingAgents behavior.
- Do NOT auto-create Decision Sessions.
- Do NOT place real/paper/dry-run orders, watch alerts, or order intents.
- Do NOT change Prefect scheduler or news-ingestor push schedules.
- Do NOT execute `push-pending --execute`.
- Do NOT print or commit credentials/tokens/connection strings.
- No production deploy; PR/CI only.
- Do NOT add a market column to `news_articles` or migrate news data.
- Do NOT widen `market_scope` beyond `kr` in this PR — the route still validates `Literal["kr"]`.

---

## Acceptance criteria

- `GET /trading/api/preopen/latest?market_scope=kr` (authenticated) returns `news` (object or null) and `news_preview` (array, ≤5).
- When ingestion is fresh: `news.status == "ready"`, `news.warnings == []`, preview lists ≤5 latest articles ordered DESC by `published_at`.
- When `is_stale` or `news_stale` is in warnings: `news.status == "stale"`, the existing `news_stale` warning continues to appear in `source_warnings`.
- When `latest_run is None` or `news_unavailable` warning is present: `news.status == "unavailable"`, preview is `[]`.
- When the readiness call itself raises: `news` is `null`, `news_preview` is `[]`, and `source_warnings` includes `news_readiness_unavailable`.
- The forbidden-imports test still passes for both `app.routers.preopen` and `app.services.preopen_dashboard_service`.
- Auth: unauthenticated → 401 unchanged. `market_scope` validation unchanged (rejects `us`).
- Preopen page renders a "News readiness" section with a status badge for all three states. Stale and unavailable show explicit, human-readable text — not silently hidden. Existing CTA (Open session / Create decision session two-click) behavior unchanged.
- All listed test commands pass on `feature/ROB-55-preopen-news-readiness`.

---

## Sonnet implementer handoff

**You are Claude Sonnet executing this plan in the same session, after the planner finishes.**

- Working directory: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-55-preopen-news-readiness`
- Branch: `feature/ROB-55-preopen-news-readiness` (already checked out in this worktree).
- Read this plan top-to-bottom, then read the files in the "Existing code to read first" list before any edits.
- Follow the tasks in order. Each task is small and ends with a commit. Do not batch commits.
- Use the bash commands as written. Use `uv run pytest` for backend, `npm test -- --run` and `npx tsc --noEmit` for frontend (run from `frontend/trading-decision/`).
- TDD where the plan provides a failing test first (Tasks 2 and 3). For UI tasks, write the component then add tests (Task 10) — the existing repo pattern uses RTL after the component exists.
- DO NOT touch: KIS, Upbit, broker, order, intent, watch, token, credential modules. The `test_no_forbidden_imports` test enforces this.
- DO NOT add any LLM call, summarization step, or NewsSignal extraction.
- DO NOT modify Prefect schedulers, news-ingestor service, or `app/routers/news_analysis.py`.
- DO NOT push to `main` or `production`. Open a PR against `main`.
- If a test fails for an unexpected reason, stop and surface the failure rather than papering over it. The most likely failure modes:
  - The router test fixture in `tests/test_router_preopen.py` may already monkeypatch the dashboard service. Read it carefully before adding the new sibling test — you may only need to extend the existing fixture/payload.
  - The `_news_readiness_payload()` helper returns tz-naive KST datetimes. Make sure new tests use `datetime.now(UTC)` only when the readiness mock is a `SimpleNamespace` (no validator). Inside the actual schema, those will be naive — that is fine.
  - If `npm run typecheck` is not defined, use `npx tsc --noEmit`.
- After all tasks pass, run the full Verification block, then push and open the PR. Stop. Do not merge. Do not deploy.
