# ROB-188 — /invest/feed/news Toss-style tabs, low-relevance filter, news row v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/invest/feed/news` visibly closer to Toss Invest News — relabel/reorder the primary tab strip to Toss semantics, drop low-relevance no-symbol items from the default tab, suppress duplicative or unanchored issue chips, and convert the news row toward Toss's dense layout (thumbnail placeholder, de-emphasized summary button) — without any DB migration, broker mutation, scheduler change, or schema break for older clients.

**Architecture:** Tighten the existing view-model `app/services/invest_view_model/feed_news_service.py::build_feed_news` filter using fields already on every `FeedNewsItem` (`relatedSymbols`, `scope`, `noiseReason`). Add an issue-chip suppression branch for unanchored / title-duplicating items. On the frontend, change the primary `NEWS_TABS` array to the Toss 5-tab row (holdings, watchlist, top, latest, hot) with Toss labels and demote `kr/us/crypto` out of the primary row (keep the API enum unchanged so deep links and existing callers still work). Restyle `NewsListItem` to add a thumbnail placeholder slot and hide the round 요약 button when there is no snippet (existing behavior) plus de-emphasize it when there is one. Extend the existing read-only smoke and vitest harnesses.

**Tech Stack:** Python 3.13 / FastAPI / Pydantic v2 / SQLAlchemy async (backend), pytest + pytest-asyncio (backend tests), React + Vite + TypeScript + react-router-dom (frontend), vitest + @testing-library/react (frontend tests), Make targets (`make test`, `make lint`).

**Worktree:** `/Users/mgh3326/worktrees/auto_trader/rob-188-toss-news-v2` (must use; do NOT touch `/Users/mgh3326/services/auto_trader/current` or `/Users/mgh3326/work/auto_trader`).

**Base branch for PR:** `main` (per repo policy in CLAUDE.md). Use one PR.

---

## Safety confirmation (read first)

This plan does NOT:
- Mutate broker/order/watch/order-intent/live/paper state, or call KIS / Upbit / Alpaca write APIs.
- Run any production DB `UPDATE`/`DELETE`/`INSERT` or backfill.
- Add or run Alembic migrations.
- Change Prefect / scheduler cadence.
- Loosen the read-only UI/API safety boundary.

It DOES:
- Edit pure view-model code (`feed_news_service.py`) — no SQL writes.
- Edit pure frontend files (TS/TSX) — no API contract removal; only additive UI changes.
- Add unit tests (backend pytest, frontend vitest).
- Extend the existing read-only `scripts/news_feed_readonly_smoke.py` (GET-only) to cover `tab=top`.
- Open one PR, monitor CI, merge to `main`, then a single production deploy + read-only smoke.

---

## Contract summary (what stays the same vs. what changes)

| Surface | Before | After |
|---|---|---|
| `FeedTab` enum (Python + TS) | `top, latest, hot, holdings, watchlist, kr, us, crypto` | **unchanged** (8 values) |
| `FeedNewsItem` schema fields | `id, title, ..., scope, tags, category, noiseReason` | **unchanged** (no new field) |
| `/invest/api/feed/news` query params | `tab, limit, cursor, includeQuotes` | **unchanged** |
| `tab=top` server-side filter | drops items where `noiseReason` starts with `kr_` AND no `relatedSymbols` | **drops items where `relatedSymbols == []` AND `scope` not in `{market_wide, kr_market_wide, mixed}`** (stricter Toss-style default) |
| `tab=hot` server-side filter | drops items where `noiseReason` starts with `kr_` AND no `relatedSymbols` | same stricter rule as `top` |
| `tab=latest` / `tab=kr` server-side filter | same KR-noise drop | **unchanged** (keep more permissive, this tab IS "everything newest") |
| Issue chip on item | suppressed only when `market=="kr"` AND `noiseReason` ∈ KR-confirmed-noise | suppressed additionally when (a) `relatedSymbols == []`, OR (b) `issue.issue_title` equals article `title` (after trim/casefold) |
| Frontend primary tab row | 8 buttons including `kr/us/crypto` | **5 buttons** in order: `holdings, watchlist, top, latest, hot` with Toss labels: `보유주식, 관심주식, 주요뉴스, 최신뉴스, 급상승뉴스` |
| Frontend secondary tab row | n/a | none in this slice (deep links to `?tab=kr`, `?tab=us`, `?tab=crypto` still work; just not shown in the row) |
| Frontend news row | header chips · title · 요약 round button · related-symbol chips · issue chip | adds thumbnail placeholder block left of title; 요약 button moved to bottom row and rendered as a small text affordance, only when `summarySnippet` exists |

False-positive / false-negative cautions:

- **FP risk (over-filtering on `top`):** A legitimate macro article that lacks a `stock_symbol` and lacks any KR scorer hit (e.g., short-form English-language macro snippet on a KR feed) will be `scope=symbol_specific` (default), so the new rule will drop it. Mitigated because such items still appear on `latest` (and `kr` if applicable). The Toss primary tabs do not promise full coverage.
- **FN risk (still leaking on `top`):** An ETF/lifestyle finance piece that picks up enough `KR_INVEST_KEYWORDS` to be marked `scope=kr_market_wide` will still show. Acceptable in v2 — out of scope per Linear: "No ranking-score overhaul beyond explicit tab labels/contracts and minimal query behavior needed to reduce visible noise." A follow-up ticket can tighten the KR scorer thresholds.
- **FP risk (issue chip suppression on duplicate-title):** Two articles with the same headline (wire copies) may all suppress their chip; intended — the chip would be redundant.
- **Cursor stability:** The filter runs in-memory after pagination, so a page may return fewer than `limit` items. This already happens today (existing crypto/kr filter) — no change.

---

## File map (which files this plan touches)

Backend:
- Modify: `app/services/invest_view_model/feed_news_service.py` (lines 471–523, plus a small helper near 70 for title normalization)
- Test (modify + add): `tests/test_invest_feed_news_router.py`

Smoke:
- Modify: `scripts/news_feed_readonly_smoke.py` (add `tab=top` to `_DEFAULT_PATHS`)
- Modify: `tests/test_news_feed_readonly_smoke.py` (update the expected path list)

Frontend:
- Modify: `frontend/invest/src/components/news/NewsTabs.tsx` (split `NEWS_TABS` into `PRIMARY_NEWS_TABS` and `SECONDARY_NEWS_TABS`; default render shows primary only; relabel)
- Modify: `frontend/invest/src/components/news/NewsListItem.tsx` (add thumbnail placeholder slot; demote 요약 button to a text affordance under the row)
- Modify: `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx` and `frontend/invest/src/pages/mobile/MobileFeedNewsPage.tsx` (no behavior change required, but verify default `tab="top"` still renders correctly with the new tab list)
- Test (modify): `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx`
- Test (add): `frontend/invest/src/__tests__/NewsTabs.test.tsx`
- Test (add): `frontend/invest/src/__tests__/NewsListItem.test.tsx`

No new files outside tests/components. No schema, no migration, no router signature change.

---

## Task 1 — Backend: stricter `top`/`hot` default filter

**Files:**
- Modify: `app/services/invest_view_model/feed_news_service.py:508-523`
- Test: `tests/test_invest_feed_news_router.py`

**Why:** Today, `top` and `hot` only drop `noiseReason` rows. ETF intro / lifestyle finance / no-symbol articles that the KR scorer doesn't flag still leak. We tighten by requiring either a related symbol or a market-wide scope tag.

- [ ] **Step 1: Add failing test — top tab drops low-relevance no-symbol article**

Append to `tests/test_invest_feed_news_router.py`:

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_drops_low_relevance_no_symbol(monkeypatch) -> None:
    """ROB-188: a KR article with no related symbols and no market-wide scope
    must NOT appear on the Toss-style default tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=801,
            market="kr",
            symbol=None,
            title="ETF 입문 — 처음 시작하는 분을 위한 가이드",
            summary="ETF는 거래소에 상장된 펀드입니다.",
            keywords=["ETF", "입문"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == []
```

- [ ] **Step 2: Add failing test — top tab keeps kr_market_wide article**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_keeps_kr_market_wide(monkeypatch) -> None:
    """ROB-188: a KR macro/index article scored kr_market_wide (no symbol but
    investment-relevant) must remain visible on tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=802,
            market="kr",
            symbol=None,
            title="코스피, 외국인 매수에 2,800선 회복",
            summary="코스피가 외국인 순매수에 힘입어 상승 마감.",
            keywords=["코스피", "외국인"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [802]
    assert resp.items[0].scope == "kr_market_wide"
```

- [ ] **Step 3: Add failing test — top tab keeps symbol-anchored article**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_top_tab_keeps_symbol_anchored(monkeypatch) -> None:
    """ROB-188: a US article with an anchored symbol must stay on tab=top."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=803, market="us", symbol="NVDA", name="NVIDIA"),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert [i.id for i in resp.items] == [803]
```

- [ ] **Step 4: Run the three new tests to verify they fail**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v -k "top_tab_drops_low_relevance_no_symbol or top_tab_keeps_kr_market_wide or top_tab_keeps_symbol_anchored"`
Expected: 1 FAIL (top_tab_drops_low_relevance_no_symbol) + 2 PASS-then-FAIL behavior (the kr_market_wide / anchored ones may pass today but will need to survive the new filter). All three tests will pass after Step 5.

- [ ] **Step 5: Tighten the filter in `feed_news_service.py`**

Replace `feed_news_service.py:508-523` (the current `if tab == "crypto": ... elif tab in ("top", "latest", "hot", "kr"): ...` block) with:

```python
    # ROB-188: Toss-style primary tabs (top / hot) must not include
    # low-relevance items that have no anchoring related symbol. We allow:
    #   - any item with at least one relatedSymbol, OR
    #   - any item whose scope marks it as broad market context
    #     (kr_market_wide from KR scorer, market_wide / mixed from US scope).
    # crypto / latest / kr keep the prior, more permissive behavior so the
    # "everything newest" feel of latest/kr is preserved.
    _DEFAULT_TAB_KEEP_SCOPES = {"market_wide", "kr_market_wide", "mixed"}

    if tab == "crypto":
        items = [i for i in items if not (i.noiseReason and not i.relatedSymbols)]
    elif tab in ("top", "hot"):
        items = [
            i
            for i in items
            if i.relatedSymbols or i.scope in _DEFAULT_TAB_KEEP_SCOPES
        ]
    elif tab in ("latest", "kr"):
        items = [
            i
            for i in items
            if not (
                i.noiseReason
                and i.noiseReason.startswith("kr_")
                and not i.relatedSymbols
            )
        ]
```

Define `_DEFAULT_TAB_KEEP_SCOPES` at module level (above `build_feed_news`) to avoid re-creating the set per request:

```python
# ROB-188 — scopes that justify keeping an item on Toss-style default tabs
# (top/hot) even when no related symbol is attached.
_DEFAULT_TAB_KEEP_SCOPES: frozenset[str] = frozenset(
    {"market_wide", "kr_market_wide", "mixed"}
)
```

…and reference `_DEFAULT_TAB_KEEP_SCOPES` (drop the local re-declaration inside `build_feed_news`).

- [ ] **Step 6: Run the three new tests to verify they pass**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v -k "top_tab_drops_low_relevance_no_symbol or top_tab_keeps_kr_market_wide or top_tab_keeps_symbol_anchored"`
Expected: 3 PASS.

- [ ] **Step 7: Update two existing test fixtures so they still assert the new contract**

Two existing tests construct a bare KR article with no `stock_symbol` and use `tab="top"`. Under the new filter both items would be dropped (`relatedSymbols == []` and `scope == "symbol_specific"` by default). Update each fixture to anchor a symbol so they continue to exercise their intended assertions (issue linkage and "no issue" behavior).

In `tests/test_invest_feed_news_router.py:89-117` (`test_feed_news_top_tab`), change the fixture row to:

```python
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr", symbol="005930", name="삼성전자"),
    ]
```

In `tests/test_invest_feed_news_router.py:282-304` (`test_feed_news_no_issue_means_none`), change the fixture row to:

```python
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=99, market="kr", symbol="005930", name="삼성전자"),
    ]
```

Leave the rest of both tests intact — `test_feed_news_top_tab`'s `issueId == "iss-1"` and `relation == "none"` still hold (`Issue iss-1` ≠ `news 1`, so Task 2's duplicate-title suppression does not trigger), and `test_feed_news_no_issue_means_none`'s `issueId is None` still holds (no issue exists for article 99 in the mocked `build_market_issues` response).

- [ ] **Step 8: Run the full feed_news test module**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v`
Expected: all tests PASS (existing + 3 new ROB-188 tests).

- [ ] **Step 9: Commit**

```bash
git add app/services/invest_view_model/feed_news_service.py tests/test_invest_feed_news_router.py
git commit -m "feat(ROB-188): drop low-relevance no-symbol items from top/hot feed tabs"
```

---

## Task 2 — Backend: suppress unanchored / duplicate-title issue chips

**Files:**
- Modify: `app/services/invest_view_model/feed_news_service.py` (suppress_issue logic at lines 471-497, plus a small normalizer helper)
- Test: `tests/test_invest_feed_news_router.py`

**Why:** Toss avoids labelling an article with a chip that just repeats the headline, and a chip with no symbol context provides no navigation value beyond the headline itself.

- [ ] **Step 1: Add failing test — issue chip suppressed when there are no related symbols**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_issue_chip_suppressed_when_no_related_symbols(
    monkeypatch,
) -> None:
    """ROB-188: an item with no relatedSymbols should NOT carry an issueId
    (the chip would be unanchored and just repeat headline noise)."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=901,
            market="kr",
            symbol=None,
            title="코스피, 외국인 매수에 2,800선 회복",
            summary="코스피가 외국인 순매수에 힘입어 상승 마감.",
            keywords=["코스피", "외국인"],
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

    issue = _fake_issue(issue_id="iss-901", article_ids=[901], market="kr")
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="top", limit=30, cursor=None
    )

    assert resp.items[0].issueId is None
```

- [ ] **Step 2: Add failing test — issue chip suppressed when issue title duplicates article title**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_issue_chip_suppressed_when_title_duplicates(
    monkeypatch,
) -> None:
    """ROB-188: if the issue's issue_title exactly matches the article title
    (after trim), suppress the chip — it would just repeat the headline."""
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=902,
            market="us",
            symbol="AAPL",
            name="Apple",
            title="Apple shares rise after iPhone update",
        ),
    ]
    summary_result = MagicMock()
    summary_result.all.return_value = []
    db.execute = AsyncMock(
        side_effect=[scalar_result, summary_result, _empty_related_result()]
    )

    # MarketIssue.issue_title is built by _fake_issue as "Issue <id>". Override
    # via a direct MarketIssue with the same title as the article.
    issue = MarketIssue(
        id="iss-902",
        market="us",
        rank=1,
        issue_title="Apple shares rise after iPhone update",
        subtitle=None,
        direction="neutral",
        source_count=1,
        article_count=1,
        updated_at=_NOW,
        articles=[
            MarketIssueArticle(
                id=902,
                title="Apple shares rise after iPhone update",
                url="https://example.com/902",
                source="Reuters",
                feed_source="rss_test",
                published_at=_NOW,
            )
        ],
        signals=IssueSignals(
            recency_score=0.5,
            source_diversity_score=0.5,
            mention_score=0.5,
        ),
    )
    monkeypatch.setattr(
        svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
    )

    resp = await svc.build_feed_news(
        db=db, resolver=RelationResolver(), tab="latest", limit=30, cursor=None
    )

    assert resp.items[0].issueId is None
```

- [ ] **Step 3: Run the two new tests to verify they fail**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v -k "issue_chip_suppressed_when_no_related_symbols or issue_chip_suppressed_when_title_duplicates"`
Expected: 2 FAIL.

- [ ] **Step 4: Add a small title-normalizer helper near `_summary_snippet_for_row`**

In `feed_news_service.py` add near line 71 (after `_summary_snippet_for_row`):

```python
def _normalize_for_title_match(value: str | None) -> str:
    """ROB-188 — normalize headline strings for duplicate-issue-chip check.

    We strip whitespace and casefold so trivial spacing/case differences do
    not prevent the suppression. We intentionally do NOT do heavier NLP
    here — a stricter equality check yields fewer false positives.
    """
    if not value:
        return ""
    return " ".join(value.split()).casefold()
```

- [ ] **Step 5: Extend the `suppress_issue` branch around lines 471-497**

Replace the current `_KR_CONFIRMED_NOISE = ... ; suppress_issue = (...)` block with:

```python
        # Suppress the issue chip when any of:
        #   1. ROB-169 — confirmed KR noise (society/crime/sport/celebrity).
        #   2. ROB-188 — no related symbol to anchor on; chip provides no
        #      navigation value beyond the headline itself.
        #   3. ROB-188 — issue title is the same as the article headline; the
        #      chip would just repeat the headline.
        _KR_CONFIRMED_NOISE = {
            "kr_crime",
            "kr_society",
            "kr_noise",
            "kr_no_invest_signal",
        }
        candidate_issue_id = issue_id_for_article.get(row.id)
        suppress_issue = (
            (market_value == "kr" and item_noise_reason in _KR_CONFIRMED_NOISE)
            or not related
            or (
                candidate_issue_id is not None
                and any(
                    _normalize_for_title_match(iss.issue_title)
                    == _normalize_for_title_match(row.title)
                    for iss in issues
                    if iss.id == candidate_issue_id
                )
            )
        )
```

…and change the `issueId=` line in the `FeedNewsItem(...)` constructor from `issue_id_for_article.get(row.id)` to `candidate_issue_id`:

```python
                issueId=None if suppress_issue else candidate_issue_id,
```

(The lookup is now done once, above the suppression check.)

- [ ] **Step 6: Run the two new tests to verify they pass**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v -k "issue_chip_suppressed_when_no_related_symbols or issue_chip_suppressed_when_title_duplicates"`
Expected: 2 PASS.

- [ ] **Step 7: Run the full feed_news test module to confirm no regressions**

Run: `uv run pytest tests/test_invest_feed_news_router.py -v`
Expected: all tests PASS, including the existing `test_feed_news_top_tab` (now with a symbol) and `test_feed_news_latest_tab_links_issue` (which has a different issue title than article title, so chip survives).

- [ ] **Step 8: Commit**

```bash
git add app/services/invest_view_model/feed_news_service.py tests/test_invest_feed_news_router.py
git commit -m "feat(ROB-188): suppress unanchored and title-duplicate issue chips"
```

---

## Task 3 — Backend smoke: cover `tab=top`

**Files:**
- Modify: `scripts/news_feed_readonly_smoke.py:20-24`
- Modify: `tests/test_news_feed_readonly_smoke.py:67-98`

**Why:** Smoke today only covers `latest, us, crypto`. The new default filter on `top` is the most behaviorally important change; we need a GET-only smoke against `top` for the post-deploy check.

- [ ] **Step 1: Update the test for the expected `_DEFAULT_PATHS` order**

In `tests/test_news_feed_readonly_smoke.py:92-97`, change:

```python
    assert [path for path, _ in calls] == [
        "/invest/api/feed/news?tab=latest&limit=20",
        "/invest/api/feed/news?tab=us&limit=20",
        "/invest/api/feed/news?tab=crypto&limit=20",
    ]
```

to:

```python
    assert [path for path, _ in calls] == [
        "/invest/api/feed/news?tab=top&limit=20",
        "/invest/api/feed/news?tab=latest&limit=20",
        "/invest/api/feed/news?tab=us&limit=20",
        "/invest/api/feed/news?tab=crypto&limit=20",
    ]
```

- [ ] **Step 2: Run the smoke test module to verify it fails**

Run: `uv run pytest tests/test_news_feed_readonly_smoke.py -v -k "test_run_smoke_uses_get_only_fetcher"`
Expected: FAIL (call list mismatch).

- [ ] **Step 3: Update `_DEFAULT_PATHS` in the smoke script**

In `scripts/news_feed_readonly_smoke.py:20-24`, change:

```python
_DEFAULT_PATHS = (
    "/invest/api/feed/news?tab=latest&limit=20",
    "/invest/api/feed/news?tab=us&limit=20",
    "/invest/api/feed/news?tab=crypto&limit=20",
)
```

to:

```python
_DEFAULT_PATHS = (
    # ROB-188 — top is the Toss-style default tab and exercises the new
    # stricter relatedSymbols/scope filter; smoke it first.
    "/invest/api/feed/news?tab=top&limit=20",
    "/invest/api/feed/news?tab=latest&limit=20",
    "/invest/api/feed/news?tab=us&limit=20",
    "/invest/api/feed/news?tab=crypto&limit=20",
)
```

- [ ] **Step 4: Run the smoke test module to verify it passes**

Run: `uv run pytest tests/test_news_feed_readonly_smoke.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/news_feed_readonly_smoke.py tests/test_news_feed_readonly_smoke.py
git commit -m "chore(ROB-188): cover tab=top in /invest/api/feed/news GET smoke"
```

---

## Task 4 — Frontend: Toss-style primary tab row

**Files:**
- Modify: `frontend/invest/src/components/news/NewsTabs.tsx`
- Test: `frontend/invest/src/__tests__/NewsTabs.test.tsx` (new)

**Why:** Linear spec: "primary tabs show: 보유주식, 관심주식, 주요뉴스, 최신뉴스, 급상승뉴스." Hide `kr/us/crypto` from the primary row (deep links still work since the FeedTab union is unchanged).

- [ ] **Step 1: Add failing test — primary tab row exposes the 5 Toss labels in order**

Create `frontend/invest/src/__tests__/NewsTabs.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { test, expect, vi } from "vitest";
import { NewsTabs, PRIMARY_NEWS_TABS } from "../components/news/NewsTabs";

test("renders the 5 Toss-style primary tabs in order", () => {
  render(<NewsTabs value="top" onChange={vi.fn()} />);
  const buttons = screen.getAllByRole("button");
  const labels = buttons.map((b) => b.textContent?.trim());
  expect(labels).toEqual([
    "보유주식",
    "관심주식",
    "주요뉴스",
    "최신뉴스",
    "급상승뉴스",
  ]);
});

test("PRIMARY_NEWS_TABS keys are the 5 Toss tabs in order", () => {
  expect(PRIMARY_NEWS_TABS.map((t) => t.key)).toEqual([
    "holdings",
    "watchlist",
    "top",
    "latest",
    "hot",
  ]);
});

test("kr/us/crypto are NOT in the primary row", () => {
  render(<NewsTabs value="top" onChange={vi.fn()} />);
  expect(screen.queryByTestId("tab-kr")).toBeNull();
  expect(screen.queryByTestId("tab-us")).toBeNull();
  expect(screen.queryByTestId("tab-crypto")).toBeNull();
});
```

- [ ] **Step 2: Run vitest to verify failure**

Run from `frontend/invest`: `pnpm test --run NewsTabs`
Expected: FAIL — `PRIMARY_NEWS_TABS` not exported; current labels are different.

- [ ] **Step 3: Replace `NewsTabs.tsx` with split primary/secondary tab arrays**

Replace the top of `frontend/invest/src/components/news/NewsTabs.tsx` (lines 1-12) with:

```tsx
import type { FeedTab } from "../../types/feedNews";

export const PRIMARY_NEWS_TABS: { key: FeedTab; label: string }[] = [
  { key: "holdings", label: "보유주식" },
  { key: "watchlist", label: "관심주식" },
  { key: "top", label: "주요뉴스" },
  { key: "latest", label: "최신뉴스" },
  { key: "hot", label: "급상승뉴스" },
];

// ROB-188 — kept exported for deep-link callers and future secondary filter
// row. NOT rendered by `<NewsTabs />` by default.
export const SECONDARY_NEWS_TABS: { key: FeedTab; label: string }[] = [
  { key: "kr", label: "국내" },
  { key: "us", label: "해외" },
  { key: "crypto", label: "크립토" },
];

// Backwards-compat alias for any caller that still imports NEWS_TABS.
// Points to the primary row. New code should import PRIMARY_NEWS_TABS.
export const NEWS_TABS = PRIMARY_NEWS_TABS;
```

Then inside the component body, replace both `NEWS_TABS.map(...)` calls (lines 29 and 69) with `PRIMARY_NEWS_TABS.map(...)`.

Leave the rest of the file (styles, `variant` switch, `data-testid="tab-${t.key}"` attributes) unchanged.

- [ ] **Step 4: Run vitest to verify pass**

Run from `frontend/invest`: `pnpm test --run NewsTabs`
Expected: 3 PASS.

- [ ] **Step 5: Update the existing DesktopFeedNewsPage test if it relied on `tab-kr`/`tab-us`/`tab-crypto`**

Open `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` and grep for `tab-kr`, `tab-us`, `tab-crypto`, or the strings `국내`, `해외`, `크립토`. If any assertions reference clicking those tabs, change them to a primary-row tab (e.g., switch the test that clicks `tab-kr` to click `tab-latest` instead, and update the fetch-arg assertion to `tab: "latest"`). If no such assertions exist, no change needed.

Run: `pnpm test --run DesktopFeedNewsPage`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/components/news/NewsTabs.tsx frontend/invest/src/__tests__/NewsTabs.test.tsx frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx
git commit -m "feat(ROB-188): show Toss-style 5-tab primary row on /invest/feed/news"
```

---

## Task 5 — Frontend: news row v2 layout (thumbnail placeholder + demoted summary)

**Files:**
- Modify: `frontend/invest/src/components/news/NewsListItem.tsx`
- Test: `frontend/invest/src/__tests__/NewsListItem.test.tsx` (new)

**Why:** Linear spec: "thumbnail placeholder or existing fallback area if no thumbnail data exists yet; remove or de-emphasize the circular 요약 button from default rows." We add a 56×56 px placeholder column to the left of the title (left-aligned, square, neutral background) so the row visually resembles Toss without ingesting `og:image` data in this slice. The 요약 button moves under the row as a small text affordance and is only rendered when there is a snippet.

- [ ] **Step 1: Add failing test — row exposes a thumbnail-placeholder element**

Create `frontend/invest/src/__tests__/NewsListItem.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { test, expect, vi } from "vitest";
import { NewsListItem } from "../components/news/NewsListItem";
import type { FeedNewsItem } from "../types/feedNews";

function makeItem(overrides: Partial<FeedNewsItem> = {}): FeedNewsItem {
  return {
    id: 1,
    title: "Headline",
    publisher: "Reuters",
    feedSource: "rss_test",
    publishedAt: new Date().toISOString(),
    market: "us",
    sourceMarket: "us",
    relatedSymbols: [
      {
        symbol: "AAPL",
        market: "us",
        displayName: "Apple",
        relation: "held",
        matchReason: "stock_symbol",
        matchedTerm: null,
        quote: { changeRate: 1.2 },
      },
    ],
    relation: "held",
    url: "https://example.com",
    issueId: null,
    summarySnippet: null,
    ...overrides,
  };
}

test("renders a thumbnail placeholder slot for every row (no ingest required)", () => {
  render(
    <MemoryRouter>
      <NewsListItem item={makeItem()} open={false} onToggle={vi.fn()} />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("feed-item-thumbnail")).toBeInTheDocument();
});

test("does NOT render the 요약 button when there is no summarySnippet", () => {
  render(
    <MemoryRouter>
      <NewsListItem item={makeItem({ summarySnippet: null })} open={false} onToggle={vi.fn()} />
    </MemoryRouter>,
  );
  expect(screen.queryByTestId("feed-item-summary-button")).toBeNull();
});

test("renders a demoted text 요약 affordance when summarySnippet exists", () => {
  render(
    <MemoryRouter>
      <NewsListItem
        item={makeItem({ summarySnippet: "snippet body" })}
        open={false}
        onToggle={vi.fn()}
      />
    </MemoryRouter>,
  );
  const button = screen.getByTestId("feed-item-summary-button");
  expect(button).toBeInTheDocument();
  expect(button.tagName.toLowerCase()).toBe("button");
  expect(button.textContent?.trim()).toMatch(/요약/);
});
```

- [ ] **Step 2: Run vitest to verify failure**

Run from `frontend/invest`: `pnpm test --run NewsListItem`
Expected: FAIL — `feed-item-thumbnail` not in DOM; the existing round button uses no `data-testid` of that name.

- [ ] **Step 3: Add thumbnail placeholder and demote the 요약 button in `NewsListItem.tsx`**

In `frontend/invest/src/components/news/NewsListItem.tsx`:

(a) Replace the inner article body's main wrapper (the block starting at line 142 `<div style={{ display: "flex", flexDirection: "column", gap: 7 }}>` through the related-symbols/issue-chip block at line 252) with the structure below. The intent: outer flex row with thumbnail on the left (constant width) and the existing content stack on the right; move the 요약 button out of the title row into a small footer under the related-symbol/issue-chip line.

```tsx
        <div style={{ display: "flex", gap: 10 }}>
          {/* ROB-188 — thumbnail placeholder. We don't ingest og:image in this
             slice (non-goal in Linear), but reserving the column matches the
             Toss dense row layout. Future tickets can render real images here. */}
          <div
            data-testid="feed-item-thumbnail"
            aria-hidden
            style={{
              flex: "0 0 auto",
              width: variant === "mobile" ? 56 : 64,
              height: variant === "mobile" ? 56 : 64,
              borderRadius: 10,
              background: "var(--surface-2)",
              border: "1px solid var(--divider)",
            }}
          />
          <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                flexWrap: "wrap",
                color: "var(--fg-3)",
                fontSize: 12,
                lineHeight: 1.35,
              }}
            >
              <span>{source}</span>
              <span aria-hidden>·</span>
              <span data-testid="feed-item-source-market">{MARKET_LABEL[feedMarket]}</span>
              <span aria-hidden>·</span>
              <span>{ago}</span>
              {relationLabel && (
                <Pill tone={relationTone(item.relation)} size="sm">
                  {relationLabel}
                </Pill>
              )}
            </div>

            <a
              href={item.url}
              target="_blank"
              rel="noreferrer"
              style={{
                color: "var(--fg)",
                fontSize: variant === "mobile" ? 15 : 16,
                fontWeight: 800,
                lineHeight: 1.38,
                letterSpacing: "-0.02em",
                textDecoration: "none",
              }}
            >
              {item.title}
            </a>

            {(item.relatedSymbols.length > 0 || issue) && (
              <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                {item.relatedSymbols.length > 0 && (
                  <div
                    data-testid="feed-item-related-symbols"
                    style={{ display: "flex", flexWrap: "wrap", gap: 6, minWidth: 0 }}
                  >
                    {item.relatedSymbols.map((symbol) => (
                      <SymbolChip key={`${symbol.market}:${symbol.symbol}`} symbol={symbol} />
                    ))}
                  </div>
                )}

                {issue && (
                  <Link
                    to={issueHref!}
                    data-testid="feed-item-issue-chip"
                    data-issue-id={issue.id}
                    aria-label={`이슈 링크: ${issue.issue_title}`}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      minHeight: 24,
                      padding: "2px 10px",
                      borderRadius: 999,
                      background: "var(--accent-soft)",
                      color: "var(--accent-press)",
                      fontSize: 11,
                      fontWeight: 700,
                      textDecoration: "none",
                      maxWidth: "100%",
                    }}
                  >
                    <span aria-hidden style={{ fontSize: 9 }}>
                      ●
                    </span>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      이슈 · {issue.issue_title}
                    </span>
                  </Link>
                )}
              </div>
            )}

            {hasSummary && (
              <button
                type="button"
                data-testid="feed-item-summary-button"
                onClick={onToggle}
                aria-expanded={open}
                aria-controls={summaryId}
                aria-label={summaryButtonLabel}
                style={{
                  alignSelf: "flex-start",
                  border: "none",
                  background: "transparent",
                  padding: 0,
                  color: "var(--fg-3)",
                  fontFamily: "inherit",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                  textDecoration: "underline",
                }}
              >
                {open ? "요약 접기" : "요약 보기"}
              </button>
            )}
          </div>
        </div>
```

(b) Remove the now-unused round-button block (the `<button type="button" onClick={onToggle} disabled={!hasSummary} ...>` that was at lines 184-206) — it has been replaced by the text affordance above.

(c) The collapsible summary block (lines 255-270, `{open && item.summarySnippet && (...)}`) stays as-is — it still uses `summaryId` and is gated by `open`.

- [ ] **Step 4: Run vitest to verify pass**

Run from `frontend/invest`: `pnpm test --run NewsListItem`
Expected: 3 PASS.

- [ ] **Step 5: Run the existing DesktopFeedNewsPage test to confirm no regression**

Run from `frontend/invest`: `pnpm test --run DesktopFeedNewsPage`
Expected: PASS. (The existing test renders the page with a summarySnippet present, so the new text button is rendered and the prior round-button assertion is gone — if the existing test asserted on `"요약"` text it will still match.)

If the existing test asserts on the round `요약` button's behavior, update those assertions to use `data-testid="feed-item-summary-button"` instead of role/text selectors that targeted the round button shape.

- [ ] **Step 6: Commit**

```bash
git add frontend/invest/src/components/news/NewsListItem.tsx frontend/invest/src/__tests__/NewsListItem.test.tsx
git commit -m "feat(ROB-188): news row v2 — thumbnail placeholder + demoted 요약 affordance"
```

---

## Task 6 — Verify mobile/desktop pages still render

**Files:**
- Read-only check: `frontend/invest/src/pages/mobile/MobileFeedNewsPage.tsx` and `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx`

**Why:** Defaults haven't changed (`tab="top"`), but the new tab strip + new row layout could surface unexpected layout regressions when the page renders an empty state or when `tab=holdings` is the active tab and there is no holding.

- [ ] **Step 1: Run the full frontend vitest suite**

Run from `frontend/invest`: `pnpm test --run`
Expected: all tests PASS.

- [ ] **Step 2: Run the frontend typecheck + build**

Run from `frontend/invest`:
```bash
pnpm typecheck
pnpm build
```
Expected: both succeed with no new errors. (If the build is wired via the repo `Makefile`, prefer `make` targets; check the README for the exact incantation.)

- [ ] **Step 3: No commit needed if no edits — skip otherwise.**

---

## Task 7 — Backend full-suite + lint + typecheck

**Files:** none (verification only)

- [ ] **Step 1: Run lint + typecheck + tests**

Run from repo root:
```bash
make lint
make typecheck
make test
```
Expected: all green. If `make test` is too slow locally, scope to `tests/test_invest_feed_news_router.py` + `tests/test_news_feed_readonly_smoke.py` for fast iteration and rely on CI to run the full suite.

- [ ] **Step 2: Fix anything red, then commit fixes under the same PR.**

```bash
git add <touched files>
git commit -m "fix(ROB-188): address lint/typecheck/test feedback"
```

---

## Task 8 — Open PR, monitor CI, merge

**Files:** none (git/gh only)

- [ ] **Step 1: Push branch and open PR against `main`**

```bash
git push -u origin HEAD
gh pr create --base main --title "ROB-188: Toss-style /invest/feed/news tabs, filter, and row v2" \
  --body "$(cat <<'EOF'
## Summary
- Tighten default-tab filter (top/hot) to drop low-relevance items with no related symbol unless scope is market_wide / kr_market_wide / mixed.
- Suppress issue chip when the article has no related symbols or when the issue title duplicates the article title.
- Primary tab row on /invest/feed/news now shows 5 Toss-style tabs: 보유주식, 관심주식, 주요뉴스, 최신뉴스, 급상승뉴스. kr/us/crypto remain reachable via deep links but are hidden from the primary row.
- News row v2: thumbnail placeholder + 요약 demoted to a text affordance below the row.
- Smoke script covers tab=top.
- No DB / broker / scheduler / KIS / Upbit / Alpaca mutations. No Alembic migration.

## Test plan
- [ ] `uv run pytest tests/test_invest_feed_news_router.py -v`
- [ ] `uv run pytest tests/test_news_feed_readonly_smoke.py -v`
- [ ] `pnpm --dir frontend/invest test --run`
- [ ] CI green
- [ ] Post-merge: production deploy
- [ ] Post-deploy: `uv run python -m scripts.news_feed_readonly_smoke --base-url <prod>` returns ok=true for tab=top/latest/us/crypto
EOF
)"
```

- [ ] **Step 2: Monitor CI with `gh pr checks --watch` and fix any red checks under this same PR (do NOT push --force; use new commits).**

- [ ] **Step 3: Merge after CI is green**

```bash
gh pr merge --squash --auto
```

Confirm merge SHA, capture the PR URL for the final report.

---

## Task 9 — Production deploy + smoke

**Files:** none

- [ ] **Step 1: Trigger the standard `main` → `production` deploy per repo convention.**

This is the deploy mechanism described in CLAUDE.md / DEPLOYMENT.md (merge `main` into `production` to trigger the GHCR image build). Do this ONCE after merge — not per-task.

- [ ] **Step 2: Wait for deploy to settle, then run the read-only smoke against production**

```bash
uv run python -m scripts.news_feed_readonly_smoke --base-url https://<prod-host>
```

If the prod endpoint requires an Authorization header, set it via env var and pass `--auth-header-env <VAR_NAME>` so the value is never printed.

Expected: all 4 paths return `ok=true`; warnings list may include `quote_partial_failure:*` (acceptable, non-fatal).

- [ ] **Step 3: Capture smoke output to evidence file**

```bash
mkdir -p .smoke-out
uv run python -m scripts.news_feed_readonly_smoke --base-url https://<prod-host> > .smoke-out/rob-188-prod.json
```

Attach the captured JSON to the Kanban final report (do not commit secrets; the script never prints auth values).

---

## Self-review (planner — performed at write time)

**Spec coverage check (against Linear ROB-188 Acceptance criteria):**

| Acceptance criterion | Where covered |
|---|---|
| Primary tabs show: 보유주식, 관심주식, 주요뉴스, 최신뉴스, 급상승뉴스 | Task 4 |
| Default Toss-like tabs do not show low-relevance no-symbol articles | Task 1 |
| ETF 입문 / 보험료 환급 / lifestyle absent from default feed | Task 1 (regression test = test_feed_news_top_tab_drops_low_relevance_no_symbol uses ETF intro fixture) |
| 반도체 / 삼성전자 / 하이닉스 / NVIDIA-related stays visible | Task 1 (regression test = test_feed_news_top_tab_keeps_symbol_anchored) |
| Issue chips do not repeat headline, and not on low-relevance rows | Task 2 |
| Row layout: title + chip + publisher/time; summary button removed/de-emphasized | Task 5 |
| Backend/frontend tests pass | Tasks 1, 2, 4, 5, 7 |
| PR + CI + merge + single prod deploy + smoke | Tasks 8, 9 |
| Worktree at `/Users/mgh3326/worktrees/auto_trader/rob-188-toss-news-v2` | Plan header |
| No broker/order/watch/scheduler/DB mutations | Safety section at top |

**Placeholder scan:** no TBD / TODO / "implement later" / "similar to Task N" / "add appropriate error handling" present. All code blocks are concrete.

**Type consistency check:**
- `PRIMARY_NEWS_TABS` defined in Task 4 step 3, referenced in Task 4 step 3 (component body) and Task 4 step 1 (test import) — names match.
- `_DEFAULT_TAB_KEEP_SCOPES` defined once at module level in Task 1 step 5; referenced once in the same block.
- `_normalize_for_title_match` defined in Task 2 step 4; referenced in Task 2 step 5.
- `data-testid="feed-item-thumbnail"` set in Task 5 step 3; asserted in Task 5 step 1.
- `data-testid="feed-item-summary-button"` set in Task 5 step 3; asserted in Task 5 step 1.
- Backend `FeedTab` and `FeedNewsScope` literals match the existing schema (`app/schemas/invest_feed_news.py:12, 20`).

---

## Exact instructions for K2 implementer

1. Confirm you are in the worktree `/Users/mgh3326/worktrees/auto_trader/rob-188-toss-news-v2`. Do NOT touch `/Users/mgh3326/services/auto_trader/current` or `/Users/mgh3326/work/auto_trader`.
2. Branch off `main` if not already on a feature branch: `git switch -c feature/ROB-188-toss-news-v2 main`.
3. Execute Tasks 1 → 9 in order. Each task ends in a commit (one logical change per commit). Do NOT batch all tasks into one commit.
4. Stop and ask the planner/reviewer if any test fails for a reason the plan didn't anticipate — do not silently widen filters or relax assertions.
5. Before opening the PR, run the full local suite at least once: `make lint && make typecheck && make test && (cd frontend/invest && pnpm test --run && pnpm typecheck && pnpm build)`.
6. Open ONE PR base `main` (Task 8). Monitor CI. Fix red checks via NEW commits in the same PR; never `--force` push, never use `--no-verify`.
7. After CI green and merge, perform ONE production deploy (Task 9) and capture the smoke output to `.smoke-out/rob-188-prod.json`.
8. Final report should include: PR URL, merge SHA, deploy timestamp, smoke result summary, and any caveats.

**Model preference (per Linear ROB-188):** implementer should use Claude Code Sonnet. If the executing harness cannot enforce model selection, record the limitation explicitly in the K2 task summary; this does not block execution.
