# ROB-148 — Link News-Related Stock Issues to News Cards

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate `FeedNewsItem.issueId` on `/invest/api/feed/news` items by mapping each article to its clustered market issue, then render an issue chip (with title + direction + link to issue detail) on the desktop news cards.

**Architecture:** In `feed_news_service.build_feed_news`, always build `MarketIssue`s for the relevant market window (currently only `top`/`hot` build them); compute an `article_id → issue_id` map from `MarketIssue.articles[*].id`; populate `FeedNewsItem.issueId` from the map; return the `MarketIssue[]` on every tab so the frontend can join client-side. The desktop news card renders an issue chip using that join.

**Tech Stack:** Python 3.13 + Pydantic + SQLAlchemy async + FastAPI on the backend; React 18 + TypeScript + Vitest + Testing Library on the frontend.

**Branch / Worktree:** Working directory `/Users/robin/.superset/worktrees/auto_trader/invest-feed-rob-146` on branch `invest-feed-rob-146` (already an isolated worktree).

---

## File Structure

**Modify:**
- `app/services/invest_view_model/feed_news_service.py` — add `_collect_issues_for_market` helper, build article→issue map, populate `issueId` on each item, and return `issues` for all tabs (not just `top`/`hot`).
- `tests/test_invest_feed_news_router.py` — add coverage for `issueId` population and for issues being returned on non-top tabs.
- `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx` — render an issue chip (link to `/app/discover/issues/{issueId}`) when an item has `issueId` and a matching issue exists in `data.issues`.
- `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` — assert chip renders + links to the issue detail page.

**Already-correct (no edits required):**
- `app/schemas/invest_feed_news.py` — `FeedNewsItem.issueId` field already exists; `FeedNewsResponse.issues` already exists.
- `frontend/invest/src/types/feedNews.ts` — `issueId?: string | null` already declared; `issues: MarketIssue[]` already declared.

---

## Task 1: Backend — populate issueId and return issues for all tabs

**Files:**
- Modify: `app/services/invest_view_model/feed_news_service.py`

- [ ] **Step 1.1: Update `build_feed_news` to always build market issues**

  Replace the existing block

  ```python
      # Hot issues for top tab.
      issues = []
      if tab in ("top", "hot"):
          try:
              issues_resp = await build_market_issues(
                  market="all", window_hours=24, limit=10
              )
              issues = issues_resp.items
          except Exception:
              issues = []
  ```

  with

  ```python
      # Build market issues for the relevant window so each news item can be
      # linked to its clustered issue (ROB-148). For market-scoped tabs we
      # filter by that market; for other tabs we cluster across markets so
      # items from any market can be linked.
      issues_market = market_filter or "all"
      try:
          issues_resp = await build_market_issues(
              market=issues_market, window_hours=24, limit=20
          )
          issues = issues_resp.items
      except Exception:
          issues = []
  ```

- [ ] **Step 1.2: Build the article→issue map and populate `issueId`**

  Insert the following block immediately after `analysis_map` is populated and before the `items: list[FeedNewsItem] = []` line:

  ```python
      # ROB-148 — article_id → issue_id map for chip rendering.
      issue_id_for_article: dict[int, str] = {}
      for issue in issues:
          for article in issue.articles:
              # Keep the highest-ranked issue per article.
              issue_id_for_article.setdefault(article.id, issue.id)
  ```

  Then, in the `FeedNewsItem(...)` constructor inside the `for row in rows:` loop, add the `issueId` field:

  ```python
          items.append(
              FeedNewsItem(
                  id=row.id,
                  title=row.title,
                  publisher=row.source,
                  feedSource=row.feed_source,
                  publishedAt=row.article_published_at,
                  market=cast(NewsMarket, market_value),
                  relatedSymbols=related,
                  issueId=issue_id_for_article.get(row.id),
                  summarySnippet=analysis_map.get(row.id) or row.summary,
                  relation=relation,
                  url=row.url,
              )
          )
  ```

- [ ] **Step 1.3: Confirm `meta.warnings` still empty / unchanged**

  No change. The `FeedNewsMeta(emptyReason=empty_reason)` call at the bottom remains as-is.

---

## Task 2: Backend tests

**Files:**
- Modify: `tests/test_invest_feed_news_router.py`

- [ ] **Step 2.1: Add `_fake_issue` helper at top of test file (after `_fake_article`)**

  Add the helper:

  ```python
  def _fake_issue(*, issue_id: str, article_ids: list[int], market: str = "kr") -> MagicMock:
      issue = MagicMock()
      issue.id = issue_id
      issue.market = market
      issue.articles = [MagicMock(id=aid) for aid in article_ids]
      return issue
  ```

- [ ] **Step 2.2: Update `test_feed_news_top_tab` to assert `issueId` is populated when an issue covers the article**

  Replace the existing `test_feed_news_top_tab` function body so that the mocked `build_market_issues` returns an issue covering article id `1`, and assert `resp.items[0].issueId == "iss-1"` and `resp.issues[0].id == "iss-1"`:

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_feed_news_top_tab(monkeypatch) -> None:
      from app.services.invest_view_model import feed_news_service as svc

      db = MagicMock()
      scalar_result = MagicMock()
      scalar_result.scalars.return_value.all.return_value = [
          _fake_article(id=1, market="kr"),
      ]
      summary_result = MagicMock()
      summary_result.all.return_value = []
      db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

      issue = _fake_issue(issue_id="iss-1", article_ids=[1], market="kr")
      monkeypatch.setattr(
          svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
      )

      resolver = RelationResolver()
      resp = await svc.build_feed_news(
          db=db, resolver=resolver, tab="top", limit=30, cursor=None
      )
      assert resp.tab == "top"
      assert len(resp.items) == 1
      assert resp.items[0].id == 1
      assert resp.items[0].issueId == "iss-1"
      assert resp.items[0].relation == "none"
      assert [i.id for i in resp.issues] == ["iss-1"]
  ```

- [ ] **Step 2.3: Add a new test asserting `issueId` is also populated on the `latest` tab**

  Add at the bottom of the file:

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_feed_news_latest_tab_links_issue(monkeypatch) -> None:
      from app.services.invest_view_model import feed_news_service as svc

      db = MagicMock()
      scalar_result = MagicMock()
      scalar_result.scalars.return_value.all.return_value = [
          _fake_article(id=42, market="us", symbol="AAPL", name="Apple"),
      ]
      summary_result = MagicMock()
      summary_result.all.return_value = []
      db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

      issue = _fake_issue(issue_id="iss-42", article_ids=[42], market="us")
      monkeypatch.setattr(
          svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[issue]))
      )

      resolver = RelationResolver()
      resp = await svc.build_feed_news(
          db=db, resolver=resolver, tab="latest", limit=30, cursor=None
      )
      assert resp.items[0].issueId == "iss-42"
      assert [i.id for i in resp.issues] == ["iss-42"]
  ```

- [ ] **Step 2.4: Add a test asserting `issueId` is `None` when no issue covers the article**

  Add at the bottom of the file:

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_feed_news_no_issue_means_none(monkeypatch) -> None:
      from app.services.invest_view_model import feed_news_service as svc

      db = MagicMock()
      scalar_result = MagicMock()
      scalar_result.scalars.return_value.all.return_value = [
          _fake_article(id=99, market="kr"),
      ]
      summary_result = MagicMock()
      summary_result.all.return_value = []
      db.execute = AsyncMock(side_effect=[scalar_result, summary_result])

      monkeypatch.setattr(
          svc, "build_market_issues", AsyncMock(return_value=MagicMock(items=[]))
      )

      resolver = RelationResolver()
      resp = await svc.build_feed_news(
          db=db, resolver=resolver, tab="top", limit=30, cursor=None
      )
      assert resp.items[0].issueId is None
  ```

- [ ] **Step 2.5: Run backend tests**

  ```bash
  uv run pytest tests/test_invest_feed_news_router.py -v
  ```

  Expected: all tests in this file pass (the existing 3 + 2 new = 5 total, with the existing `test_feed_news_holdings_empty_when_no_holdings` and `test_feed_news_assigns_held_relation` still passing).

  If any unrelated test in the same file fails because `build_market_issues` is no longer guarded by `tab in ("top", "hot")`, patch `svc.build_market_issues` with `AsyncMock(return_value=MagicMock(items=[]))` in those tests too. (`test_feed_news_holdings_empty_when_no_holdings` and `test_feed_news_assigns_held_relation` already do, but verify.)

---

## Task 3: Frontend — render issue chip on news cards

**Files:**
- Modify: `frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx`

- [ ] **Step 3.1: Add `Link` import**

  At the top of the file, add:

  ```tsx
  import { Link } from "react-router-dom";
  ```

- [ ] **Step 3.2: Build an issue lookup map and render chip when item has a matching issue**

  Inside the component, just before the `return` statement, add:

  ```tsx
    const issueById = new Map((data?.issues ?? []).map((i) => [i.id, i] as const));
  ```

  Then, inside the `(data?.items ?? []).map((it) => { ... })` block, replace the inner `<div style={{ fontSize: 11, color: "#9ba0ab", marginTop: 4 }}>` line with an expanded version that also renders the issue chip:

  ```tsx
                    <div style={{ fontSize: 11, color: "#9ba0ab", marginTop: 4 }}>
                      {it.publisher ?? "—"} · {it.market.toUpperCase()}
                      {it.relation !== "none" && <span style={{ marginLeft: 8 }}>[{it.relation}]</span>}
                    </div>
                    {it.issueId && issueById.get(it.issueId) && (
                      <Link
                        to={`/app/discover/issues/${it.issueId}`}
                        data-testid="feed-item-issue-chip"
                        data-issue-id={it.issueId}
                        onClick={(e) => e.stopPropagation()}
                        style={{
                          marginTop: 6,
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "2px 8px",
                          borderRadius: 999,
                          background: "var(--surface-2, #1c1e24)",
                          color: "#cfd2da",
                          fontSize: 11,
                          textDecoration: "none",
                          maxWidth: "100%",
                        }}
                      >
                        <span aria-hidden style={{ fontSize: 9 }}>●</span>
                        <span
                          style={{
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          이슈 · {issueById.get(it.issueId)!.issue_title}
                        </span>
                      </Link>
                    )}
  ```

  Note: the chip must be rendered **outside** the existing `<button>` element (which is the title click area) — place it directly inside the `<li>`, after the `<button>`, but before the `{open && it.summarySnippet && ...}` summary expansion. This avoids nesting an interactive `<Link>` inside a `<button>` (invalid HTML).

  Concretely the resulting `<li>` body should look like:

  ```tsx
                <li ...>
                  <button ...>
                    {/* title + meta */}
                  </button>
                  {it.issueId && issueById.get(it.issueId) && (
                    <Link ... />
                  )}
                  {open && it.summarySnippet && (
                    <div ... />
                  )}
                </li>
  ```

---

## Task 4: Frontend tests

**Files:**
- Modify: `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx`

- [ ] **Step 4.1: Update the mocked `fetchFeedNews` response to include an issue and link the item to it**

  Replace the `vi.spyOn(feedApi, "fetchFeedNews")...` block in `beforeEach` with:

  ```tsx
    vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue({
      tab: "top",
      asOf: new Date().toISOString(),
      issues: [
        {
          id: "iss-xyz",
          market: "kr",
          rank: 1,
          issue_title: "삼성전자 실적 발표",
          subtitle: null,
          direction: "up",
          source_count: 3,
          article_count: 2,
          updated_at: new Date().toISOString(),
          summary: null,
          related_symbols: [],
          related_sectors: [],
          articles: [
            {
              id: 1,
              title: "n1",
              url: "x",
              source: "Reuters",
              feed_source: null,
              published_at: null,
              summary: null,
              matched_terms: [],
            },
          ],
          signals: { recency_score: 1, source_diversity_score: 1, mention_score: 1 },
        },
      ],
      items: [
        {
          id: 1,
          title: "n1",
          market: "kr",
          relatedSymbols: [],
          relation: "none",
          url: "x",
          publisher: "Reuters",
          issueId: "iss-xyz",
        },
      ],
      meta: { warnings: [] },
    });
  ```

- [ ] **Step 4.2: Add a test asserting the issue chip renders with the expected title and link**

  Add to the test file, after the existing `test("renders news items and reacts to tab change", ...)` block:

  ```tsx
  test("renders an issue chip linked to the issue detail page when issueId is present", async () => {
    render(
      <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
        <DesktopFeedNewsPage />
      </MemoryRouter>,
    );
    const chip = await screen.findByTestId("feed-item-issue-chip");
    expect(chip).toHaveTextContent("삼성전자 실적 발표");
    expect(chip).toHaveAttribute("href", "/invest/app/discover/issues/iss-xyz");
    expect(chip).toHaveAttribute("data-issue-id", "iss-xyz");
  });
  ```

- [ ] **Step 4.3: Run the frontend tests**

  ```bash
  cd frontend/invest && npm test -- --run DesktopFeedNewsPage.test.tsx
  ```

  Expected: both tests pass.

---

## Task 5: Verification, lint, format, commit

- [ ] **Step 5.1: Run all relevant Python tests**

  ```bash
  uv run pytest tests/test_invest_feed_news_router.py tests/test_invest_view_model_safety.py -v
  ```

  Expected: all pass.

- [ ] **Step 5.2: Run backend lint + format**

  ```bash
  make lint
  make format
  ```

  Expected: no errors.

- [ ] **Step 5.3: Run all frontend invest tests**

  ```bash
  cd frontend/invest && npm test -- --run
  ```

  Expected: all pass.

- [ ] **Step 5.4: Commit**

  ```bash
  git add app/services/invest_view_model/feed_news_service.py \
          tests/test_invest_feed_news_router.py \
          frontend/invest/src/pages/desktop/DesktopFeedNewsPage.tsx \
          frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx \
          docs/superpowers/plans/2026-05-08-rob-148-link-news-issues-to-cards.md
  git commit -m "$(cat <<'EOF'
  feat(invest): link news cards to clustered issues (ROB-148)

  - feed_news_service builds market issues for every tab and maps
    article_id -> issue_id so FeedNewsItem.issueId is populated
  - DesktopFeedNewsPage renders an issue chip per news card linking
    to /app/discover/issues/:issueId

  Read-only path; no broker / order / watch mutations.

  Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
  EOF
  )"
  ```

- [ ] **Step 5.5: Push and open PR**

  ```bash
  git push -u origin invest-feed-rob-146
  gh pr create --base main --title "feat(invest): link news cards to clustered issues (ROB-148)" --body "$(cat <<'EOF'
  ## Summary
  - Populate `FeedNewsItem.issueId` on `/invest/api/feed/news` by joining each article into the clustered `MarketIssue.articles` from `build_market_issues`.
  - Return the `issues` array on every tab (not just `top`/`hot`) so the desktop feed can render a chip per news card.
  - Render an issue chip on each desktop news card linking to `/app/discover/issues/:issueId`.

  Closes ROB-148.

  ## Test plan
  - [x] `uv run pytest tests/test_invest_feed_news_router.py -v`
  - [x] `cd frontend/invest && npm test -- --run DesktopFeedNewsPage.test.tsx`
  - [x] `make lint`

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

  Expected: PR URL printed; capture for the final report.

---

## Self-Review Checklist

- Spec coverage: every news card now exposes the linked issue both in API (`issueId`) and UI (chip). ✓
- Placeholder scan: every step has concrete code/commands; no TBDs. ✓
- Type consistency: `issueId` field name used in backend Pydantic model, frontend type, and chip rendering matches. `MarketIssue.id` (string) is the join key. ✓
- Existing test `test_feed_news_holdings_empty_when_no_holdings` already passes `MagicMock(items=[])` for `build_market_issues`, so it remains compatible after Task 1.1's removal of the `tab in ("top", "hot")` guard. ✓
- The chip is rendered **outside** the title `<button>` to avoid nesting interactive elements (HTML validity / a11y). ✓
- Read-only invariant from project CLAUDE.md preserved: only view-model service + frontend changes; no broker / order / watch mutations. ✓
