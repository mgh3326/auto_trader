# Screener analyst-consensus cache (ROB-686)

`screen_stocks_snapshot`'s `min_analyst_*` filters and the per-page KR analyst
consensus (buy/hold/sell counts, target prices, analyst label) are now backed
by a per-symbol Redis cache-aside instead of live-scraping Naver research
pages (`company_list.naver` + up to 10 `company_read.naver`) on every call.
This runbook is the operator note for the cache: key format, TTL, the shared
kill switch, fail-open behavior, scope, and how to invalidate.

> **Migration-0.** This is a Redis cache-aside layer only — no new DB
> table/column, no alembic revision. The pre-existing `analyst_consensus_snapshots`
> precompute table (ROB-641, `app/models/analyst_consensus_snapshot.py`) is a
> separate surface (scoped to holdings ∪ active watch) and is not read or
> written by this cache.

---

## 1. What is cached (and what is NOT)

Only the **daily-stable** analyst consensus fields are cached:
`buy_count`, `hold_count`, `sell_count`, `strong_buy_count`, `total_count`,
`avg_target_price`, `median_target_price`, `min_target_price`,
`max_target_price`, `rows_total`, `rows_used`, `rows_excluded_stale`,
`rows_undated`, `newest_opinion_date`, `window_months`, `target_price_count`,
`target_price_honest`.

`current_price` and `upside_pct` are **never cached** — they are stripped
before the cache write and recomputed on every call from a fresh
`_fetch_current_price` (a single `item/main.naver` GET), so the displayed
target-upside never serves a stale intraday price even when the consensus
counts themselves are a cache hit.

A consensus with `total_count <= 0` is never persisted (a network blip must
not poison the day with a fake "no coverage" verdict) — genuinely
zero-coverage discovery symbols are re-fetched live on every call. This is a
known, accepted tradeoff (see the implementation plan's "Known tradeoff"
note); a future refinement could distinguish fetch-failed from
fetch-succeeded-but-empty and cache the latter.

## 2. Key format + TTL

```
screener_consensus:naver:{SYMBOL}:{YYYY-MM-DD in KST}
```

- Namespace prefix `screener_consensus:` is **distinct** from the
  analyze-path fetch cache's `analyze_fetch:` prefix (ROB-638,
  `app/core/analyze_cache.py`) — the two caches never collide even though
  they share the same Redis client + TTL helpers.
- TTL matches the ROB-638 `naver` provider TTL: before the KRX session close
  (15:35 KST) the entry lives until today's close; after close it lives
  until the next KST midnight.
- KR only. US (yfinance) consensus is never cached — `market == "us"` always
  calls the live `handle_get_investment_opinions` (behaviorally unchanged
  from before this change).

## 3. Env / config gate

`ANALYZE_FETCH_CACHE_ENABLED` (shared with the ROB-638 analyze-path cache;
`settings.analyze_fetch_cache_enabled`, default `true` in production,
force-set to `false` in `tests/conftest.py`).

- `false` → `analyze_cache._get_redis_client()` returns `None` →
  every consensus cache/get/set call is a no-op → every symbol falls open to
  a direct live fetch on every call (today's pre-ROB-686 behavior).
- `true` → the cache-aside path is active.

There is no dedicated env var for this cache — it deliberately reuses the
same gate as the analyze-path cache so there is exactly one on/off switch
for "does auto_trader ever cache a Naver fetch."

## 4. Fail-open behavior

Every helper in `app/services/invest_view_model/analyst_consensus_cache.py`
degrades to a live fetch and never raises:

- Redis unavailable / connection error on GET or SET → treated as a miss /
  no-op, logged at `debug`.
- Malformed cached payload (not a JSON dict) → treated as a miss.
- Live fetch failure/exception → the symbol is simply absent from the
  filter-counts map (dropped by `min_analyst_*`) or returns
  `{"error": "analyst_consensus_unavailable"}` from the page provider —
  same shape as the pre-existing `enrich_snapshot_page` error path.
- Fresh-price fetch failure at page-enrichment time → `current_price`/
  `upside_pct` are simply omitted from the response rather than serving a
  stale value.

## 5. How to invalidate

```bash
docker compose exec redis redis-cli --scan --pattern "screener_consensus:*"
# review the keys, then:
docker compose exec redis redis-cli --scan --pattern "screener_consensus:*" | xargs -n 1 redis-cli DEL
```

Because the key embeds the KST date, no manual invalidation is normally
required — entries roll over automatically at the next session-close/midnight
boundary. Manual invalidation is only useful to force an immediate re-fetch
(e.g. after a broker data correction) within the same trading day.

## 6. Scope / what did NOT change

- `asyncio.Semaphore(4)` in both `enrich_snapshot_page` and the new
  `resolve_consensus_counts` resolver is unchanged — Naver was already
  throttling; this fix reduces request *count*, not concurrency.
- The consensus fetch limit stays `limit=10` (matches the pre-existing
  `enrich_snapshot_page` / `_opinion_payload` call) — raising it would change
  `totalCount` filter semantics and triple cold `company_read.naver` fetches
  per symbol.
- `app/services/naver_finance/investor.py` / `parser.py` and the ROB-641
  precompute stack (`app/services/analyst_consensus_snapshots/**`,
  `app/jobs/analyst_consensus_snapshots.py`) are untouched.
- Read-only advisory screener: no broker/order/watch/order-intent mutation
  on this path.
