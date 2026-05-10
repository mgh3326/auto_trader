# ROB-172 News Source-Market vs Related-Asset-Market Separation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate the **source/feed market** of a news article from the **market of each related asset** in the `/invest/api/feed/news` payload, so that a `kr`-feed article like `엔비디아` (news_articles.id=9659, market=kr, feed_source=browser_naver_mainnews) can surface `NVDA/us` as a related symbol — without breaking the existing `market` field consumed by the frontend.

**Architecture:** Read-layer-only fix. Two coordinated changes in one PR:

1. **Schema (additive)** — `FeedNewsItem` gains a new `sourceMarket: NewsMarket` field that mirrors the existing `market` field's semantics (the article's source/feed market). The legacy `market` field is preserved unchanged for backward compatibility with the existing frontend renderer (`NewsListItem.tsx` line 144 reads `MARKET_LABEL[item.market]`). `NewsRelatedSymbol.market` is **already** the asset's market (alias dict carries `entry.market`); we only add a docstring/Field description making this explicit.

2. **Service (semantic fix)** — `feed_news_service._related_symbols_for_article` currently calls `match_symbols_for_article(..., market=market_value)`, which restricts alias dictionary lookup to entries whose `market == article's market`. This is the bug that prevents `엔비디아` (alias of `NVDA/us`) from matching when `article.market == "kr"`. Change the call to `market=None` so `ALL_ALIASES` is searched, then trust each match's intrinsic `match.market` (the asset's market) when building `NewsRelatedSymbol`. The pre-existing `_add_related_symbol(market=match.market, ...)` path already routes the asset market correctly.

**Persistence is request-time only for this PR.** No new ingest writes, no DB migration, no backfill. The `news_article_related_symbols` row schema already supports cross-market rows (`market` column carries the asset's market via `_normalize_related_symbol_market`). A follow-up ticket can elect to (a) re-ingest under the new matcher behavior, or (b) add a backfill CLI — both deferred and out of scope.

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy 2 async, pydantic v2 (`extra="forbid"`), pytest 9 + pytest-asyncio. Frontend TypeScript (`frontend/invest/src/types/feedNews.ts`) + Vitest. All work in worktree `/Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98` on branch `feature/ROB-172-news-market-semantics` (already created, baseline at `66efba66` = `origin/main`).

**Pre-flight (one-time, before Task 1):**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
uv sync --all-groups
uv run pytest tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_invest_feed_news_router.py tests/test_news_entity_matcher.py tests/test_news_feed_readonly_smoke.py -v
# Expected: all PASS at baseline. If anything fails on 66efba66, STOP and report.
cd frontend/invest && npm install --no-audit --no-fund && npx vitest run --reporter=basic --run src/__tests__/DesktopFeedNewsPage.test.tsx
# Expected: PASS at baseline.
```

---

## File Structure

**Modified files (backend):**
- `app/schemas/invest_feed_news.py` — add `sourceMarket: NewsMarket` field to `FeedNewsItem` (additive, not a default — server populates it). Add explicit `Field(description=...)` strings clarifying that `FeedNewsItem.market`/`sourceMarket` are the **source/feed** market, while `NewsRelatedSymbol.market` is the **asset** market.
- `app/services/invest_view_model/feed_news_service.py` — (a) inside `_related_symbols_for_article`, change `match_symbols_for_article(..., market=market_value)` to `match_symbols_for_article(..., market=None)` so cross-market aliases match; (b) inside `build_feed_news`'s `for row in rows:` loop, pass `sourceMarket=market_typed` when constructing each `FeedNewsItem`. No other call-site change. The `_add_related_symbol(stock_symbol, market=market_value, ...)` shortcut at lines 158–166 keeps `market_value` because `row.stock_symbol` is by construction a same-market anchor written by the per-market ingestor.

**Modified files (frontend):**
- `frontend/invest/src/types/feedNews.ts` — add `sourceMarket?: "kr" | "us" | "crypto"` to `FeedNewsItem` (optional during the dual-emission window so older mocks don't break the type checker). Add a TSDoc comment on `FeedRelatedSymbol.market` clarifying it is the asset's market.
- `frontend/invest/src/components/news/NewsListItem.tsx` — **no rendering change in this PR.** A short comment in `MARKET_LABEL` notes that the source-market label will migrate to `item.sourceMarket ?? item.market` in a follow-up UX ticket once the backend has shipped and dashboards confirm.
- `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx` — extend the existing `feedResponse()` factory to include `sourceMarket: "kr"` on the test item, and add one assertion that the type compiles + the page still renders the legacy `KR` chip from `item.market`.

**Modified files (tests):**
- `tests/test_invest_feed_news_router.py` — add 3 new tests:
  1. `test_feed_news_kr_article_with_us_alias_emits_us_related_symbol` (the headline regression — KR feed `엔비디아` headline → relatedSymbol `NVDA/us`).
  2. `test_feed_news_emits_source_market_alongside_legacy_market` (new field present, equals legacy `market`).
  3. `test_feed_news_us_article_alias_match_unchanged` (regression — US article still matches US aliases the same way it did before).
- `tests/test_feed_news_scope.py` — add `test_feed_news_item_source_market_field_present_and_matches_market`.
- `tests/test_news_entity_matcher.py` — add `test_match_for_article_with_market_none_finds_us_alias_in_korean_text` (cross-market call-shape contract for the matcher used by the service).
- `tests/test_news_feed_readonly_smoke.py` — extend `validate_feed_payload` (the assertion lives in `scripts/news_feed_readonly_smoke.py`) to optionally check that, when `sourceMarket` is present, it equals `market`. Add 1 unit test asserting the new check.
- `scripts/news_feed_readonly_smoke.py` — add `_ADDITIVE_FIELDS` entry `"sourceMarket"` as **optional** in this PR (warn-only) so prod payloads emitted before deploy don't fail the smoke; flip to required in a follow-up after rollout.

**New files:**
- `docs/runbooks/news-source-vs-asset-market.md` — short runbook (≤120 lines) documenting the contract distinction, the example of news_articles.id=9659, and the dry-run-only inspection commands operators can use.

**Out of scope (deliberately):**
- No DB migration, no backfill execution, no Prefect/scheduler/cadence changes.
- No news-ingestor changes (cross-market `stock_candidates` writing is a separate ticket).
- No `news_articles.market` semantic change — it remains the source-market column.
- No broker / order / watch / order-intent / live / paper / KIS / Upbit mutations. No production DB writes.
- No frontend rendering label change for the source-market chip; that is a follow-up UX ticket once dashboards confirm the new contract.

---

## Task 1: Add the failing service-level regression test

**Files:**
- Test: `tests/test_invest_feed_news_router.py:599-end` (append at bottom)

This is the canonical regression: a `kr`-market article whose title contains `엔비디아` must surface `NVDA/us` as a related symbol. The existing baseline returns `[]` because `match_symbols_for_article(market="kr")` filters `US_ALIASES` out.

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_kr_article_with_us_alias_emits_us_related_symbol(
    monkeypatch,
) -> None:
    """ROB-172: a KR-feed article that names a US-aliased entity (엔비디아 →
    NVDA) must surface the US asset in relatedSymbols. Source market stays
    "kr" (it is the article's feed market), asset market is "us".
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=9659,
            market="kr",
            symbol=None,
            name=None,
            title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
            summary="엔비디아의 차세대 GPU 발표가 국내 반도체 공급망에 호재로 작용",
            keywords=["엔비디아", "반도체"],
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

    resolver = RelationResolver()
    resp = await svc.build_feed_news(
        db=db, resolver=resolver, tab="latest", limit=30, cursor=None
    )

    item = resp.items[0]
    assert item.market == "kr"  # source/feed market preserved
    assert item.sourceMarket == "kr"  # additive contract field
    related_pairs = [(s.market, s.symbol) for s in item.relatedSymbols]
    assert ("us", "NVDA") in related_pairs
    nvda = next(s for s in item.relatedSymbols if s.symbol == "NVDA")
    assert nvda.market == "us"  # asset market, not source market
    assert nvda.matchReason == "alias_dict"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
uv run pytest tests/test_invest_feed_news_router.py::test_feed_news_kr_article_with_us_alias_emits_us_related_symbol -v
```

Expected: FAIL. The failure mode is one of two:
- `AttributeError: 'FeedNewsItem' object has no attribute 'sourceMarket'` (schema not yet extended), OR
- `assert ("us", "NVDA") in related_pairs` fails because the alias was filtered out by `market="kr"`.

Both failure modes are acceptable signals that the test is correctly red.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_invest_feed_news_router.py
git commit -m "test(rob-172): failing regression — KR feed article must surface US-aliased related symbol

The headline contract bug for ROB-172: news_articles.id=9659 (market=kr,
title contains 엔비디아) currently returns relatedSymbols=[] because the
matcher is restricted to KR aliases. Asserts both the additive
sourceMarket field and the cross-market relatedSymbol."
```

---

## Task 2: Add the failing schema-level contract test

**Files:**
- Test: `tests/test_feed_news_scope.py:91-end` (append at bottom)

Pin the additive contract at the schema layer so the field's existence is independent of any service wiring.

- [ ] **Step 1: Append the failing test**

```python
def test_feed_news_item_source_market_field_present_and_matches_market():
    """ROB-172: FeedNewsItem must expose `sourceMarket` (the article's feed
    market) alongside the legacy `market` field. The two values are equal
    during the backward-compat window; once the frontend migrates, the legacy
    `market` field can be retired in a separate ticket.
    """
    item = FeedNewsItem(
        id=9659,
        title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
        market="kr",
        sourceMarket="kr",
        url="https://example.com/news/9659",
    )

    assert item.market == "kr"
    assert item.sourceMarket == "kr"
    # `extra="forbid"` must continue to reject unknown fields.
    with pytest.raises(ValidationError):
        FeedNewsItem(
            id=9659,
            title="x",
            market="kr",
            sourceMarket="kr",
            url="https://example.com/news/9659",
            unknownField=True,
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
uv run pytest tests/test_feed_news_scope.py::test_feed_news_item_source_market_field_present_and_matches_market -v
```

Expected: FAIL with `ValidationError: 1 validation error for FeedNewsItem ... sourceMarket Extra inputs are not permitted` (because `extra="forbid"` is on and the field is not yet declared).

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_feed_news_scope.py
git commit -m "test(rob-172): failing schema-level test — FeedNewsItem.sourceMarket must exist"
```

---

## Task 3: Add the failing matcher-level test for cross-market alias lookup

**Files:**
- Test: `tests/test_news_entity_matcher.py:189-end` (append at bottom)

`match_symbols_for_article(..., market=None)` is the contract the service will switch to. Lock it down here so a future refactor of `_aliases_for_market` cannot silently re-narrow the lookup.

- [ ] **Step 1: Append the failing test**

```python
@pytest.mark.unit
def test_match_for_article_with_market_none_finds_us_alias_in_korean_text():
    """ROB-172 contract: callers that omit `market` must search ALL_ALIASES
    so a KR-feed article carrying `엔비디아` resolves to NVDA/us.
    """
    matches = match_symbols_for_article(
        title="엔비디아 신제품 공개에 국내 반도체주 동반 강세",
        summary="엔비디아의 차세대 GPU 발표가 국내 반도체 공급망에 호재로 작용",
        keywords=["엔비디아", "반도체"],
        market=None,
    )
    by_symbol = {m.symbol: m for m in matches}
    assert "NVDA" in by_symbol, f"expected NVDA in matches, got {sorted(by_symbol)}"
    assert by_symbol["NVDA"].market == "us"
    assert by_symbol["NVDA"].reason == "alias_dict"
    assert by_symbol["NVDA"].matched_term == "엔비디아"
```

- [ ] **Step 2: Run to verify it passes already**

```bash
uv run pytest tests/test_news_entity_matcher.py::test_match_for_article_with_market_none_finds_us_alias_in_korean_text -v
```

Expected: **PASS**. The matcher already supports `market=None` → `ALL_ALIASES`; this test is a regression-pin that documents the contract. If it FAILS, STOP — that means the matcher contract has drifted and the service-layer fix in Task 5 will not work.

- [ ] **Step 3: Commit the regression-pin test**

```bash
git add tests/test_news_entity_matcher.py
git commit -m "test(rob-172): pin matcher contract — match_symbols_for_article(market=None) finds NVDA in Korean text"
```

---

## Task 4: Extend the schema with `sourceMarket`

**Files:**
- Modify: `app/schemas/invest_feed_news.py:37-54` (the `FeedNewsItem` class body)

Add the additive field. Keep `market` exactly as-is for backward compatibility — the frontend `NewsListItem.tsx:144` and the smoke script both still rely on it.

- [ ] **Step 1: Edit the schema**

Replace the `FeedNewsItem` class with:

```python
class FeedNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    publisher: str | None = None
    feedSource: str | None = None
    publishedAt: datetime | None = None
    # ROB-172: `market` is the article's *source/feed* market (kr/us/crypto).
    # Kept for backward compatibility; new clients should prefer `sourceMarket`,
    # which has the same value but a name that does not collide with the
    # related-asset market on `NewsRelatedSymbol`.
    market: NewsMarket = Field(
        description="Source/feed market of the article (kr/us/crypto). "
        "Backward-compatible alias for sourceMarket."
    )
    sourceMarket: NewsMarket = Field(
        description="Source/feed market of the article (kr/us/crypto). "
        "Equal to `market` during the backward-compat window."
    )
    relatedSymbols: list[NewsRelatedSymbol] = Field(default_factory=list)
    issueId: str | None = None
    summarySnippet: str | None = None
    relation: RelationKind = "none"
    url: str
    # ROB-155: additive read-layer classification fields; defaults preserve backward compat.
    scope: NewsScope = "symbol_specific"
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    noiseReason: str | None = None
```

And add a `Field(description=...)` to `NewsRelatedSymbol.market` (lines 22–24) — replace the bare `market: NewsMarket` line with:

```python
    market: NewsMarket = Field(
        description="ROB-172: the *asset's* market (e.g. NVDA→us), not the "
        "article's source market. May differ from FeedNewsItem.sourceMarket "
        "when an article in one market discusses a symbol from another."
    )
```

- [ ] **Step 2: Run the schema-contract test (Task 2) — should now PASS**

```bash
uv run pytest tests/test_feed_news_scope.py::test_feed_news_item_source_market_field_present_and_matches_market -v
```

Expected: PASS.

- [ ] **Step 3: Run all schema/scope tests to confirm no regression**

```bash
uv run pytest tests/test_feed_news_scope.py -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add app/schemas/invest_feed_news.py
git commit -m "feat(rob-172): add FeedNewsItem.sourceMarket additive field

Distinguishes the article's source/feed market from the related-asset
market carried on each NewsRelatedSymbol. The legacy `market` field is
preserved unchanged for backward compatibility with the existing
frontend renderer; new clients should prefer `sourceMarket`."
```

---

## Task 5: Service-layer fix — cross-market alias matching + sourceMarket population

**Files:**
- Modify: `app/services/invest_view_model/feed_news_service.py:168-173` (matcher call)
- Modify: `app/services/invest_view_model/feed_news_service.py:408-426` (FeedNewsItem construction)

Two surgical edits. Both are read-layer-only.

- [ ] **Step 1: Widen the matcher call inside `_related_symbols_for_article`**

Find the block (currently lines 168–173):

```python
    alias_matches = match_symbols_for_article(
        title=row.title,
        summary=analysis_summary or row.summary,
        keywords=_coerce_keywords(getattr(row, "keywords", None)),
        market=market_value,
    )
```

Replace with:

```python
    # ROB-172: search ALL_ALIASES (market=None) so an article whose source/feed
    # market is e.g. "kr" can still surface a US-aliased entity such as
    # NVDA when its Korean alias 엔비디아 appears in the title/summary/keywords.
    # `match.market` carries the asset's market, which is what
    # `_add_related_symbol` already routes onto `NewsRelatedSymbol.market`.
    alias_matches = match_symbols_for_article(
        title=row.title,
        summary=analysis_summary or row.summary,
        keywords=_coerce_keywords(getattr(row, "keywords", None)),
        market=None,
    )
```

- [ ] **Step 2: Populate `sourceMarket` when constructing each `FeedNewsItem`**

Find the `items.append(FeedNewsItem(...))` block (currently lines 408–426):

```python
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                market=market_typed,
                relatedSymbols=related,
                issueId=issue_id_for_article.get(row.id),
                summarySnippet=analysis_summary or row.summary,
                relation=relation,
                url=row.url,
                scope=cast(NewsScope, item_scope),
                tags=scope_tags,
                category=item_category,
                noiseReason=item_noise_reason,
            )
        )
```

Replace with:

```python
        items.append(
            FeedNewsItem(
                id=row.id,
                title=row.title,
                publisher=row.source,
                feedSource=row.feed_source,
                publishedAt=row.article_published_at,
                # ROB-172: `market` is the legacy alias kept for backward
                # compatibility; `sourceMarket` is the same value on the new
                # contract. Both equal `market_typed` during the backward-compat
                # window. Per-related-symbol asset markets live on
                # `NewsRelatedSymbol.market` and may differ from these.
                market=market_typed,
                sourceMarket=market_typed,
                relatedSymbols=related,
                issueId=issue_id_for_article.get(row.id),
                summarySnippet=analysis_summary or row.summary,
                relation=relation,
                url=row.url,
                scope=cast(NewsScope, item_scope),
                tags=scope_tags,
                category=item_category,
                noiseReason=item_noise_reason,
            )
        )
```

- [ ] **Step 3: Run the headline regression test (Task 1) — should now PASS**

```bash
uv run pytest tests/test_invest_feed_news_router.py::test_feed_news_kr_article_with_us_alias_emits_us_related_symbol -v
```

Expected: PASS.

- [ ] **Step 4: Run the full feed-news service test suite to confirm no regression**

```bash
uv run pytest tests/test_invest_feed_news_router.py tests/test_feed_news_scope.py tests/test_feed_news_crypto_filter.py tests/test_news_entity_matcher.py -v
```

Expected: all PASS.

If any pre-existing test fails (e.g. one that *expects* a US-only article to NOT match a KR alias because of the old `market=us` filter), STOP and report — the failure indicates we need to add a stricter "asset markets must be allowed by tab/scope" filter before widening. Inspect each failure carefully; do not silently weaken the assertion.

- [ ] **Step 5: Commit**

```bash
git add app/services/invest_view_model/feed_news_service.py
git commit -m "fix(rob-172): widen alias matching to cross-market and emit sourceMarket

Previously, match_symbols_for_article was restricted to the article's
source market, which prevented a KR-feed article like news_articles.id=9659
('엔비디아') from resolving NVDA/us. Pass market=None so ALL_ALIASES is
searched; each match still carries the asset's intrinsic market, which
NewsRelatedSymbol.market continues to surface unchanged. Also populate the
new FeedNewsItem.sourceMarket additive field with the article's source
market so frontends can disambiguate the two without name collision."
```

---

## Task 6: Add the unchanged-US-behavior regression test

**Files:**
- Test: `tests/test_invest_feed_news_router.py:end` (append after Task 1's test)

Belt and braces: confirm that widening the matcher does not change the answer for a clearly US-only article. Concretely, an article with `market=us` and an obvious US headline still gets the same single US-aliased relatedSymbol it did before.

- [ ] **Step 1: Append the test**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_us_article_alias_match_unchanged_after_cross_market(
    monkeypatch,
) -> None:
    """ROB-172 regression-guard: widening the matcher to ALL_ALIASES must not
    change the relatedSymbols answer for an obviously US-anchored article.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(
            id=9700,
            market="us",
            symbol=None,
            name=None,
            title="Amazon raises guidance on AWS demand",
            summary="Amazon Web Services revenue beat expectations.",
            keywords=["AWS", "Amazon"],
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
        db=db, resolver=RelationResolver(), tab="us", limit=30, cursor=None
    )

    item = resp.items[0]
    assert item.market == "us"
    assert item.sourceMarket == "us"
    related_pairs = [(s.market, s.symbol) for s in item.relatedSymbols]
    assert ("us", "AMZN") in related_pairs
    # No KR/crypto false-positive should appear on a clean US headline.
    assert all(s.market == "us" for s in item.relatedSymbols), related_pairs
```

- [ ] **Step 2: Run it — should PASS**

```bash
uv run pytest tests/test_invest_feed_news_router.py::test_feed_news_us_article_alias_match_unchanged_after_cross_market -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_invest_feed_news_router.py
git commit -m "test(rob-172): regression-guard — US-anchored article still matches only US aliases"
```

---

## Task 7: Add the dual-emission contract test

**Files:**
- Test: `tests/test_invest_feed_news_router.py:end` (append)

Pin that the service emits both `market` and `sourceMarket` and that they are equal during the backward-compat window. This becomes the gate that lets a future ticket retire `market` safely.

- [ ] **Step 1: Append the test**

```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_feed_news_emits_source_market_alongside_legacy_market(
    monkeypatch,
) -> None:
    """ROB-172 dual-emission contract: every FeedNewsItem returns both `market`
    and `sourceMarket`, equal in value, so the frontend can migrate readers
    incrementally without coordinated deploy.
    """
    from app.services.invest_view_model import feed_news_service as svc

    db = MagicMock()
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [
        _fake_article(id=1, market="kr"),
        _fake_article(id=2, market="us", symbol="AAPL", name="Apple"),
        _fake_article(id=3, market="crypto", symbol="BTC", name="Bitcoin"),
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
        db=db, resolver=RelationResolver(), tab="latest", limit=30, cursor=None
    )

    assert len(resp.items) == 3
    for item in resp.items:
        assert item.sourceMarket == item.market
        assert item.sourceMarket in ("kr", "us", "crypto")
```

- [ ] **Step 2: Run — should PASS**

```bash
uv run pytest tests/test_invest_feed_news_router.py::test_feed_news_emits_source_market_alongside_legacy_market -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_invest_feed_news_router.py
git commit -m "test(rob-172): pin dual-emission contract — sourceMarket equals legacy market"
```

---

## Task 8: Frontend type widening

**Files:**
- Modify: `frontend/invest/src/types/feedNews.ts:14-36` (FeedRelatedSymbol, FeedNewsItem)

Two TS edits, both additive. No render change.

- [ ] **Step 1: Edit the types**

Replace the `FeedRelatedSymbol` and `FeedNewsItem` interfaces with:

```typescript
export interface FeedRelatedSymbol {
  symbol: string;
  /**
   * ROB-172: the *asset's* market (e.g. NVDA → "us"). May differ from the
   * containing article's `sourceMarket` when a KR-feed article discusses a
   * US-listed entity and vice versa.
   */
  market: "kr" | "us" | "crypto";
  displayName: string;
  relation?: RelationKind;
  matchReason?: string | null;
  matchedTerm?: string | null;
  quote?: FeedRelatedSymbolQuote | null;
}

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  /**
   * ROB-172: source/feed market of the article. Backward-compatible alias for
   * `sourceMarket`; both fields carry the same value during the dual-emission
   * window. Existing renderers (NewsListItem.tsx) read this; new renderers
   * should prefer `sourceMarket` for clarity vs FeedRelatedSymbol.market.
   */
  market: "kr" | "us" | "crypto";
  /**
   * ROB-172: source/feed market of the article. Optional during the
   * dual-emission window — the backend always populates it; the optional
   * marker only avoids forcing every existing fixture/mock to update at once.
   */
  sourceMarket?: "kr" | "us" | "crypto";
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
}
```

- [ ] **Step 2: Type-check the frontend**

```bash
cd frontend/invest && npx tsc --noEmit
```

Expected: 0 errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
git add frontend/invest/src/types/feedNews.ts
git commit -m "feat(rob-172): widen FeedNewsItem TS type with optional sourceMarket

Documents the source-market vs asset-market distinction in TSDoc comments
on FeedRelatedSymbol.market and FeedNewsItem.market/sourceMarket. The new
field is optional in TS only so existing fixtures need not all update at
once; the backend always populates it (tested in
test_feed_news_emits_source_market_alongside_legacy_market)."
```

---

## Task 9: Frontend renderer comment + test fixture extension

**Files:**
- Modify: `frontend/invest/src/components/news/NewsListItem.tsx:14-18` (MARKET_LABEL block — add a single-line forward-pointer comment, NO behavior change)
- Modify: `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx:44-76` (extend the test fixture's `items[0]` to include `sourceMarket`)

We are deliberately not changing what the chip renders. The comment exists so the next ticket touching this file knows where to make the migration.

- [ ] **Step 1: Edit `NewsListItem.tsx`**

Replace lines 14–18:

```typescript
const MARKET_LABEL: Record<FeedNewsItem["market"], string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};
```

with:

```typescript
// ROB-172: this label reflects the source/feed market. A follow-up UX ticket
// will switch the read site to `item.sourceMarket ?? item.market` once the
// backend dual-emission has fully rolled out and dashboards confirm. Do NOT
// confuse with `symbol.market` on the related-asset chip, which is the
// asset's market.
const MARKET_LABEL: Record<FeedNewsItem["market"], string> = {
  kr: "KR",
  us: "US",
  crypto: "CRYPTO",
};
```

- [ ] **Step 2: Extend the desktop test fixture**

In `frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx`, find the `items: [...]` block inside `feedResponse()` (lines 44–76) and replace the single item with:

```typescript
    items: [
      {
        id: 1,
        title: "n1",
        market: "kr",
        sourceMarket: "kr", // ROB-172 dual-emission contract
        relatedSymbols: [
          {
            symbol: "005930",
            market: "kr",
            displayName: "삼성전자",
            relation: "watchlist",
            matchReason: "alias_dict",
            matchedTerm: "삼성전자",
            quote: { changeRate: 1.23 },
          },
          {
            symbol: "000660",
            market: "kr",
            displayName: "SK하이닉스",
            relation: "none",
            matchReason: "alias_dict",
            matchedTerm: "하닉",
          },
        ],
        relation: "watchlist",
        url: "https://example.com/n1",
        publisher: "Reuters",
        feedSource: "browser_naver_research",
        publishedAt: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
        issueId: "iss-xyz",
        summarySnippet: "삼성전자 실적 발표 요약입니다.",
      },
    ],
```

- [ ] **Step 3: Run the desktop page test suite to confirm no regression**

```bash
cd frontend/invest && npx vitest run --reporter=basic --run src/__tests__/DesktopFeedNewsPage.test.tsx
```

Expected: all 6 existing tests still PASS. The label assertion `expect(row).toHaveTextContent("KR")` is unchanged because we did not change the renderer.

- [ ] **Step 4: Commit**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
git add frontend/invest/src/components/news/NewsListItem.tsx frontend/invest/src/__tests__/DesktopFeedNewsPage.test.tsx
git commit -m "chore(rob-172): comment renderer migration seam and extend test fixture

NewsListItem.tsx renders item.market today; the comment marks the seam for
the follow-up UX ticket that will switch reads to item.sourceMarket. The
desktop test fixture is extended to include sourceMarket on the test item
so future renderer updates have a green baseline to flip against."
```

---

## Task 10: Smoke-script optional-warn for sourceMarket

**Files:**
- Modify: `scripts/news_feed_readonly_smoke.py:25` (`_ADDITIVE_FIELDS` and validator body)
- Modify: `tests/test_news_feed_readonly_smoke.py:end` (one new test)

The smoke runs read-only against prod. We do NOT want it to red-fail before the new build deploys. Add `sourceMarket` as an **optional warn** field this PR, with a clear marker that a follow-up flips it to required after rollout.

- [ ] **Step 1: Edit the smoke script**

In `scripts/news_feed_readonly_smoke.py`, find the `_ADDITIVE_FIELDS` constant (around line 25):

```python
_ADDITIVE_FIELDS = ("scope", "tags", "category", "noiseReason")
```

Leave the required-fields list unchanged. Below the constant (still inside the module-level constants block), add:

```python
# ROB-172: optional during the dual-emission window. After the backend rollout
# settles, a follow-up ticket should move "sourceMarket" into _ADDITIVE_FIELDS
# (required) and remove this constant. Do not flip in this PR.
_OPTIONAL_ADDITIVE_FIELDS_WARN = ("sourceMarket",)
```

In `validate_feed_payload`, find the `for field in _ADDITIVE_FIELDS:` loop inside the `for idx, item in enumerate(items):` block (around line 81) and append, *immediately after* that loop body:

```python
        # ROB-172: dual-emission window — warn (do not error) when sourceMarket
        # is missing or when sourceMarket != market. After rollout the warn
        # becomes a hard error in a follow-up ticket.
        for field in _OPTIONAL_ADDITIVE_FIELDS_WARN:
            if field not in item:
                warnings.append(f"item_{idx}_optional_missing_{field}")
        if "sourceMarket" in item and "market" in item and item["sourceMarket"] != item["market"]:
            warnings.append(
                f"item_{idx}_source_market_diverges_from_market:"
                f"{item.get('sourceMarket')!r}!={item.get('market')!r}"
            )
```

- [ ] **Step 2: Append a test for the warn behavior**

In `tests/test_news_feed_readonly_smoke.py`:

```python
def test_validate_feed_payload_warns_on_missing_source_market_during_dual_emission():
    result = validate_feed_payload(
        "/invest/api/feed/news?tab=latest&limit=20",
        {
            "items": [
                {
                    "id": 1,
                    "title": "Old build payload",
                    "market": "kr",
                    "url": "https://example.com/news",
                    "relatedSymbols": [],
                    "scope": "symbol_specific",
                    "tags": [],
                    "category": None,
                    "noiseReason": None,
                    # sourceMarket intentionally missing
                }
            ]
        },
    )

    assert result.ok is True  # warn-only, not a hard failure
    assert any(w.startswith("item_0_optional_missing_sourceMarket") for w in result.warnings)


def test_validate_feed_payload_warns_when_source_market_diverges_from_market():
    result = validate_feed_payload(
        "/invest/api/feed/news?tab=latest&limit=20",
        {
            "items": [
                {
                    "id": 2,
                    "title": "Diverged payload (should never happen, but warn)",
                    "market": "kr",
                    "sourceMarket": "us",  # divergent
                    "url": "https://example.com/news",
                    "relatedSymbols": [],
                    "scope": "symbol_specific",
                    "tags": [],
                    "category": None,
                    "noiseReason": None,
                }
            ]
        },
    )

    assert result.ok is True
    assert any(w.startswith("item_0_source_market_diverges_from_market") for w in result.warnings)
```

- [ ] **Step 3: Run the smoke tests**

```bash
uv run pytest tests/test_news_feed_readonly_smoke.py -v
```

Expected: all PASS, including the 2 new warn tests and the 4 pre-existing tests (the existing tests do not include `sourceMarket` in their fixtures, so they will newly emit a `optional_missing_sourceMarket` warning — confirm this does not flip `result.ok` to False; if it does, fix the validator so it remains a warning, not an error).

- [ ] **Step 4: Commit**

```bash
git add scripts/news_feed_readonly_smoke.py tests/test_news_feed_readonly_smoke.py
git commit -m "chore(rob-172): smoke-script warns on missing/divergent sourceMarket

Adds optional warn-only checks during the dual-emission window so prod
payloads emitted before the backend rollout do not red-fail the smoke. A
follow-up ticket will promote sourceMarket from warn-only to required
after rollout has settled."
```

---

## Task 11: Runbook

**Files:**
- Create: `docs/runbooks/news-source-vs-asset-market.md`

Short operator-facing reference. Read-only inspection commands plus the contract distinction.

- [ ] **Step 1: Create the runbook**

```markdown
# News source-market vs related-asset-market (ROB-172)

`/invest/api/feed/news` distinguishes two different markets per news row:

| Field | Meaning | Example |
|---|---|---|
| `FeedNewsItem.sourceMarket` (and legacy `market`) | The article's source/feed market — i.e. which ingestor pipeline produced the row. Stored as `news_articles.market`. | `kr` for an article from `browser_naver_mainnews` |
| `FeedNewsItem.relatedSymbols[].market` | The *asset's* market — which exchange the symbol trades on. Carries the alias dictionary's `entry.market`. | `us` for `NVDA` even when the article's `sourceMarket` is `kr` |

These two values are independent. A `kr` article that mentions `엔비디아` will surface a related symbol with `market="us"` and `symbol="NVDA"`, while the article's `sourceMarket` stays `"kr"`.

## Reference example

`news_articles.id=9659`:
- `market = "kr"` (source)
- `feed_source = "browser_naver_mainnews"`
- `title` contains `엔비디아`
- Before ROB-172: `relatedSymbols == []` (matcher restricted to KR aliases)
- After ROB-172: `relatedSymbols == [{symbol: "NVDA", market: "us", ...}]`

## Read-only inspection (production-safe)

The following are GET-only, never write. Confirm the smoke target before running.

```bash
# 1. Hit the feed endpoint and pretty-print one KR row.
curl -sS -H "Authorization: $AUTH_HEADER" \
  "$BASE_URL/invest/api/feed/news?tab=kr&limit=5" \
  | jq '.items[0] | {id, title, market, sourceMarket, related: [.relatedSymbols[] | {market, symbol}]}'

# 2. Run the dual-emission smoke checker (warn-only for sourceMarket in this build).
uv run python -m scripts.news_feed_readonly_smoke --base-url "$BASE_URL"
```

Expected after deploy:
- Every item has `sourceMarket` equal to `market`.
- No `item_*_optional_missing_sourceMarket` warning.
- No `item_*_source_market_diverges_from_market` warning.

## Persistence

This PR is request-time only. No DB rows are written or migrated. The
`news_article_related_symbols` table already supports cross-market rows
(its `market` column is the asset's market — see `models/news.py:157` and
the CHECK constraint at `models/news.py:130-133`), and a future ticket
may elect to backfill or update the news-ingestor `stock_candidates`
emitter to write cross-market rows at ingest time.

## Rollback

Revert the single commit `feat(rob-172): add FeedNewsItem.sourceMarket
additive field` and the matching `fix(rob-172): widen alias matching to
cross-market and emit sourceMarket`. The frontend type widening and
runbook can stay (they are inert without the backend change).
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/news-source-vs-asset-market.md
git commit -m "docs(rob-172): operator runbook for source-market vs asset-market"
```

---

## Task 12: Full-suite green check + push

**Files:**
- N/A (verification + push)

- [ ] **Step 1: Run the full backend test suite touched by this PR**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
uv run pytest \
  tests/test_invest_feed_news_router.py \
  tests/test_feed_news_scope.py \
  tests/test_feed_news_crypto_filter.py \
  tests/test_news_entity_matcher.py \
  tests/test_news_feed_readonly_smoke.py \
  -v
```

Expected: all PASS.

- [ ] **Step 2: Run lint + typecheck**

```bash
make lint
make typecheck
```

Expected: clean. Fix any drift surfaced.

- [ ] **Step 3: Run the frontend test that exercises the type widening**

```bash
cd frontend/invest && npx vitest run --reporter=basic --run src/__tests__/DesktopFeedNewsPage.test.tsx && npx tsc --noEmit
```

Expected: PASS + 0 type errors.

- [ ] **Step 4: Push the branch**

```bash
cd /Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98
git push -u origin feature/ROB-172-news-market-semantics
```

Do NOT open the PR from this plan executor — the K3 reviewer task or human will handle PR creation so the PR description can include their review notes.

---

## Self-Review

**Spec coverage check** (against the K1 planner deliverables in the task body):

| Deliverable | Where addressed |
|---|---|
| 1. Additive API contract: sourceMarket vs market, backward compat | Task 4 (schema), Task 5 step 2 (service emits both), Task 7 (dual-emission test pin) |
| 2. Backend/frontend implementation split | Backend: Tasks 4–7, 10. Frontend: Tasks 8–9. Smoke + docs: Tasks 10–11. |
| 3. Persistence decision (new-ingest+legacy fallback vs request-time only) | Architecture section + Task 11 runbook: **request-time only this PR**, no migration, no backfill, follow-up explicitly deferred |
| 4. Exact files/tests to modify | File Structure section + every task header |
| 5. Confirm no scheduler/backfill/broker side effects | "Out of scope" callout in Architecture; runbook reiterates |
| 6. Worktree/branch strategy | Architecture: `/Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98` on `feature/ROB-172-news-market-semantics` (already created at baseline `66efba66`) |

**Placeholder scan:** No "TBD", no "implement later", no "add appropriate error handling", no "similar to Task N" without showing the code. All test code blocks are complete and executable. All edits show the literal old/new code.

**Type-and-name consistency:**
- `sourceMarket` — same casing in schema (Task 4), service (Task 5 step 2), test fixtures (Tasks 1, 6, 7, 9), TS type (Task 8), smoke script (Task 10), runbook (Task 11). ✅
- `match_symbols_for_article(market=None)` matches the signature in `app/services/news_entity_matcher.py:141` (verified in pre-flight). ✅
- `_add_related_symbol(market=match.market, ...)` already routes asset market correctly — unchanged. ✅

---

## Downstream instructions

**For K2 (implementer / Sonnet):**
- Use this worktree: `/Users/mgh3326/.hermes/hermes-agent/.worktrees/t_49047b98`. Branch already created: `feature/ROB-172-news-market-semantics` from `origin/main@66efba66`.
- Execute Tasks 1 → 12 in order via `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans`.
- TDD discipline is non-negotiable: Tasks 1–3 must be committed RED before Tasks 4–5 turn them green.
- If Task 5 step 4 surfaces a pre-existing test failure that the matcher widening seems to have caused, **STOP and report**, do not weaken the assertion.
- Do NOT push to a non-`feature/ROB-172-*` branch. Do NOT open the PR — K3 will do it.

**For K3 (reviewer / Opus):**
- Verify against the headline regression: news_articles.id=9659 (KR `엔비디아`) returns `relatedSymbols` containing `{market: "us", symbol: "NVDA"}` — both via Task 1's unit test and via the read-only smoke against staging if available.
- Confirm dual-emission: `sourceMarket == market` for every item across all tabs (top/latest/hot/holdings/watchlist/kr/us/crypto). Task 7 covers latest; spot-check the other tabs by curl.
- Confirm the frontend page still renders the legacy `KR/US/CRYPTO` chip (no UX regression). Task 9 and the existing DesktopFeedNewsPage.test.tsx assertions cover this.
- Confirm safety boundary: no DB migration shipped, no Prefect change, no broker code touched, smoke remains GET-only with auth header redacted. Look for any `INSERT|UPDATE|DELETE` or `pg_insert` introduced by the diff — there should be none.
- PR title suggestion: `feat(ROB-172): separate news source market from related-asset market in /invest/feed/news`.
