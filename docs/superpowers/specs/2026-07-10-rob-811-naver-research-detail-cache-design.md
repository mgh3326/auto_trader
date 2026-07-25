# ROB-811 — DB-first cache for Naver research detail pages

**Status:** design approved (2026-07-10)
**Issue:** ROB-811 `[perf] Naver 리서치 스크레이프 DB-first 캐시`
**Priority:** High

## Problem

Sentry 24h profiling (2026-07-10) shows the shared top cost of two MCP tools is
scraping Naver research **detail** pages:

- `GET finance.naver.com/research/company_read.naver?nid=X` — **724 calls/day,
  ~590s total** across `analyze_stock_batch` (27 calls/day, avg 11.6s) and
  `screen_stocks_snapshot` (8 calls/day, avg 8.4s).
- Sentry auto-flags: "Consecutive HTTP" and "N+1 Query" on these paths.

The detail page is an **immutable published document** — a broker research
report's target price and rating are fixed at publication (a revision = a new
report = a new `nid`). Yet it is re-scraped on every tool call.

Both tool paths already have Redis caches (ROB-638 fetch cache; ROB-686
consensus cache-aside), but both are **per-symbol, per-day, TTL'd** — so each new
day / cold miss re-fetches every detail page for a symbol. What's missing is a
**durable, cross-day, per-`nid`** cache for the immutable detail pages.

## Goal

Fetch each Naver research detail page (`nid`) **at most once, ever**. Steady
state: only genuinely-new reports incur a detail GET. Analysis output must be
byte-identical to today.

## Non-goals

- Caching the **list** page (`company_list.naver`). It must stay live — it is how
  new `nid`s are discovered and needs freshness. (No short-TTL list cache in this
  change.)
- Touching the existing Redis caches (ROB-638 / ROB-686). The new cache sits
  *beneath* them.
- Any change to consensus math, rating normalization, or the public
  `/research-reports/recent` feed.

## Design

### Storage — dedicated cache table (not `research_reports`)

A new, purpose-built table. `research_reports` (ROB-140) was rejected because it
has no `nid` key and no `target_price`/`rating` columns (so it needs additive
columns either way), carries heavy ingest scaffolding (`dedup_key`,
`ingestion_run`, copyright attribution), and feeds the curated public research
feed (pollution risk).

New ORM model `app/models/naver_research_detail_cache.py`, table
`naver_research_detail_cache`:

| column | type | notes |
|---|---|---|
| `nid` | Text | **PK** |
| `target_price` | Numeric, nullable | value from `em.money strong` |
| `rating` | Text, nullable | value from `em.coment` |
| `fetched_at` | TIMESTAMP(tz), default now | observability only; **no TTL** |

Additive **new-table** migration (Alembic autogenerate). No change to any
existing schema. Operator runs `alembic upgrade head` at cutover.

### Writer boundary

Per repo convention ("all writes via a repository"), a dedicated
`NaverResearchDetailCacheRepository` is the **sole writer** for this table
(`app/services/naver_finance/detail_cache/`). It exposes:

- `get_many(nids: list[str]) -> dict[str, dict]` — one `WHERE nid IN (...)` query;
  returns `{nid: {"target_price": ..., "rating": ...}}` for hits only. Kills the
  N+1 pattern Sentry flagged.
- `put_many(rows: list[dict]) -> None` — one insert with
  `ON CONFLICT (nid) DO NOTHING` (idempotent; safe under concurrent tool runs).

A thin service/port `NaverResearchDetailCache` adapts the repository, owns its
own short-lived DB session (standard async session factory) per batch op, and
**swallows any DB error** so a cache fault degrades gracefully to uncached
scraping (never breaks analysis). Optional env gate
`NAVER_RESEARCH_DETAIL_CACHE_ENABLED` (default **true**) for no-deploy kill.

### Wiring — one change to the shared assembly

The per-`nid` detail fan-out lives in
`_build_investment_opinions_from_company_list_soup`
(`app/services/naver_finance/investor.py:106`). It already takes an injected
`detail_fetcher: Callable[[str], Awaitable[dict | None]]` and is shared by both
tool paths (`fetch_investment_opinions` → `_fetch_report_detail`; `_fetch_kr_snapshot`
→ `_fetch_report_detail_with_client`). So **one** cache insertion covers both.

Add an optional `detail_cache: DetailCachePort | None = None` param threaded
through `_build_investment_opinions_from_company_list_soup`,
`fetch_investment_opinions`, and `_fetch_kr_snapshot`. When `None`, behavior is
**identical to today** (existing tests pass untouched). When present, the
assembly:

1. Collects all `nid`s from the list page.
2. `cache.get_many(nids)` — one batch read.
3. Fans out `detail_fetcher(nid)` for **misses only** (hits skip the HTTP call).
4. Merges hits + fresh results in original order (list-row fields still come
   fresh from the live list page).
5. `cache.put_many(fresh_successes)` — one batch write.

`investor.py` depends only on a `DetailCachePort` Protocol (`get_many` /
`put_many`) — it stays DB-agnostic and unit-testable with a fake. The concrete
cache is constructed and injected at the outer call sites
(`fundamentals_sources_naver` / analyze + screen paths); a site without a usable
session passes `None` (uncached, graceful).

### Correctness rules (invariants)

1. **Cache only on successful GET+parse.** `_fetch_report_detail*` already
   returns `None` on any exception and a `dict` on success. Write a row **iff the
   fetcher returned a dict**. A `None` (fetch/HTTP failure) is **never** written —
   it must be retried next call, not poisoned as a permanent null.
2. **Success-with-no-target is cache-worthy.** A 200 page with no target element
   yields `{"target_price": None, "rating": None}` — a valid, immutable fact.
   Presence of the `nid` row = hit (null columns included), so such reports are
   not re-fetched.
3. **Byte-identical downstream.** The assembly must feed the exact same dict shape
   `{"target_price": ..., "rating": ...}` into the existing merge/`build_consensus`
   code whether the value came from cache or live. The cached read must reconstruct
   the **same Python value/type** the live parse produced (verify `parse_korean_number`
   return type; coerce the `Numeric` round-trip on read if needed). A test asserts
   cached-path opinion output == live-path output for representative fixtures.

## Testing (TDD)

Unit tests around the assembly with an injected fake cache + monkeypatched HTML:

- **Cache hit → 0 detail HTTP calls.** All `nid`s pre-seeded; assert the
  `detail_fetcher` is never invoked; output equals the live-path output.
- **Miss → fetch + write.** Empty cache; assert one `detail_fetcher` call per
  `nid` and `put_many` receives the parsed rows.
- **Fetch failure → no row written.** `detail_fetcher` returns `None`; assert
  `put_many` excludes that `nid`; opinion falls back to `target_price=None`.
- **Null-target success → row written once, then hit.** `{target_price: None}`
  is stored; a second pass makes 0 HTTP calls.
- **Mixed hit/miss** preserves original report order and consensus.
- **`detail_cache=None`** reproduces current behavior exactly (regression guard).
- Repository test: `get_many` batch read (single query), `put_many` idempotent
  under duplicate `nid` (`ON CONFLICT DO NOTHING`).

Existing `tests/test_naver_finance.py::TestFetchInvestmentOpinions` and
`TestFetchKrSnapshot` continue to pass unmodified (default `detail_cache=None`).

## Expected impact

~590s/day saved; `analyze_stock_batch` / `screen_stocks_snapshot` avg roughly
halved at steady state. Reduced Naver load / block risk. Migration-0 to existing
schema (one additive new table).

## Rollout

1. Ship ORM model + Alembic new-table migration + repository/service + wiring +
   tests behind the code path (cache defaults enabled; migration applied by
   operator).
2. Operator `alembic upgrade head`.
3. Verify via Sentry that `company_read.naver` GET/day drops toward the
   new-reports-only floor.
