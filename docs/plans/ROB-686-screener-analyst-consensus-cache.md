# Screener Analyst-Consensus Cache + Pre-Pagination Over-Enrichment Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Stop `screen_stocks_snapshot` from live-scraping Naver analyst-research pages on every call (Sentry 7d: avg 24s, p95 78s; `company_read.naver` = 6,274 calls / 8.41M ms / ~75% of all child HTTP). Route the KR analyst-consensus enrichment through a per-symbol Redis cache-aside keyed by `(market, symbol, KST-date)` (reusing the proven ROB-638 `app/core/analyze_cache` infrastructure), and fix the pre-pagination over-enrichment so the `min_analyst_*` filter resolves consensus counts once (cache-aside, bounded) instead of fully live-enriching up to 200 rows before pagination. Displayed target-upside stays intraday-fresh by recomputing `currentPrice`/`upsidePct` from a cheap fresh Naver price on the returned page only. Do NOT raise `Semaphore(4)` — the 8-9s tails mean Naver is already throttling; the win is fewer requests, not more concurrency.

**Architecture:** Today `screen_stocks_snapshot_impl` (`app/mcp_server/tooling/screener_snapshot_tool.py:198`) builds the merged preset result set, then in the `min_analyst_*` branch (`:455`) it live-enriches the **entire** matched set (capped at `_MAX_ANALYST_ENRICHMENT_ROWS = 200`, `:195`) via `enrich_snapshot_page(rows=all_results, ...)` (`:479`) *before* pagination, filters on the enriched `analysisContext.consensus` counts (`_consensus_count`, `:166`/`:488`/`:493`), then paginates. `enrich_snapshot_page` (`app/services/invest_view_model/screener_analysis_enrichment.py:346`) fans out per symbol under `asyncio.Semaphore(4)` (`:390`) calling `handle_get_investment_opinions` (its `opinion_provider` default, `:351`). For KR that dispatches to `fetch_investment_opinions` (`app/services/naver_finance/investor.py:304`) = 1 `company_list.naver` (`:329`) + up to 10 gathered `company_read.naver` (`:123`) + 1 `item/main.naver` current price (`_fetch_current_price`, `:287`/`:333`); every fetch opens a **fresh** `httpx.AsyncClient` with no cache (`app/services/naver_finance/parser.py:80`,`:92`). Nothing is cached anywhere, so every screener call re-scrapes; the `min_analyst_*` × 200-row path is the 78s p95.

Target flow: a new module `app/services/invest_view_model/analyst_consensus_cache.py` provides a KR Redis cache-aside layer over `handle_get_investment_opinions`, keyed `screener_consensus:naver:{SYMBOL}:{KST-date}` with the same session-close TTL/timezone/fail-open helpers as `analyze_cache` (`_get_redis_client`/`_fetch_cache_ttl_seconds`/`_provider_date_for_key`, guarded by `settings.analyze_fetch_cache_enabled`). Only the **daily-stable** consensus fields (buy/hold/sell/strong-buy/total counts + avg/median/min/max target prices + metadata) are cached; the volatile `current_price`/`upside_pct` are stripped before caching and **recomputed** on the returned page from a fresh `_fetch_current_price`. The tool's `min_analyst_*` branch is rewritten to (1) resolve consensus **counts** for all matched symbols via the cache-aside layer (cold entries fetched once under the existing `Semaphore(4)`, warmed into Redis + a per-call memo), (2) filter, (3) paginate, (4) `enrich_snapshot_page(rows=page, opinion_provider=<cached provider>)` for the returned page only. US (yfinance) stays exactly as-is — it is a single non-fanout call per symbol and is not implicated in the Sentry bottleneck.

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), SQLAlchemy async (read-only lazy-fill only), httpx, FastMCP tool registration, pydantic v2 (`ScreenerAnalysisConsensus`), `redis.asyncio` (via `app/core/analyze_cache` + `app/services/ohlcv_cache_common.create_redis_client`).

## Approach / decision

- **Redis cache-aside, migration-0 — NOT a new precompute table.** A precompute table already exists (`analyst_consensus_snapshots`, ROB-641: `app/models/analyst_consensus_snapshot.py`, `app/services/analyst_consensus_snapshots/{repository,builder}.py`, `app/jobs/analyst_consensus_snapshots.py`), but its nightly builder scopes symbols to **holdings ∪ active watch** (`app/jobs/analyst_consensus_snapshots.py:136` `_resolve_holdings_and_watch_symbols`), which gives poor coverage for a *discovery* screener that surfaces arbitrary new candidates. A per-symbol Redis cache-aside populates on demand for **any** discovery symbol with a daily TTL, fully covers the `min_analyst_*` filter inputs + buy/hold/sell label, and stays migration-0. Per the Global Constraints, the Redis path is preferred when it covers the filter+label needs. The ROB-641 precompute table is intentionally left untouched (it serves other consumers; broadening its scope is a possible follow-up, not this ticket).
- **Reuse `app/core/analyze_cache` infra, distinct key namespace.** The key prefix is `screener_consensus:` (NOT `analyze_fetch:`) so screener consensus payloads never collide with the analyze path's whole-snapshot bundle, while the TTL/timezone/redis-client/fail-open/hermetic-guard helpers are reused verbatim. The same `settings.analyze_fetch_cache_enabled` flag guards it (already forced `false` in `tests/conftest.py:106`), so tests stay hermetic by default.
- **KR-only scope.** All new caching + the upside recompute are gated on `market == "kr"`. US routes straight to the existing live `handle_get_investment_opinions` (uncached) via the same `resolve_consensus`/`cached_opinion_provider` path — behaviorally equivalent to today's live enrich, so US freshness is unchanged. Only the *filter* code path is new for US; the produced counts are identical to the old enriched-consensus counts.
- **Known tradeoff — zero-coverage symbols are re-fetched every call.** `set_cached_consensus` refuses to persist any consensus with `total_count <= 0` (never cache a degraded/transient-failure result). Because a *genuinely* zero-coverage symbol also has `total_count == 0`, discovery symbols with no analyst reports are re-scraped live on every screener call (the cache only warms symbols that HAVE coverage). This is the conservative choice (a network blip must not poison the day with a fake "no coverage" verdict). A safe future refinement — distinguishing a *fetch-succeeded-but-empty* consensus (`build_consensus` returns a dict with `total_count == 0`) from a *fetch-failed* one (`resolve_consensus` returns `None`) and caching only the former — is intentionally out of scope here to keep the primary fix minimal.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **FRESHNESS: the consensus payload embeds currentPrice/upsidePct. Caching the whole consensus for 1-24h makes displayed target-upside intraday-stale. Cache keyed by (market, symbol, KST-date) is correct for the min_analyst_* FILTER inputs (buyCount/totalCount are daily-stable) and the buy/hold/sell label; but recompute currentPrice-derived upside cheaply rather than serving a day-old price.**
- **Read-only advisory screener. The only write on the path is sector lazy-fill (fail-open); no broker/order/watch/order-intent mutation.**
- **If choosing the precompute path, adding a snapshot table/column IS allowed but must ship its own alembic migration and follow the InvestScreenerSnapshotsRepository write-only pattern; prefer the Redis-cache path if it fully covers the filter+label needs to stay migration-0. State the decision explicitly.** → Decision: Redis-cache path (see "Approach / decision"). **Migration-0.**
- **Do NOT raise `asyncio.Semaphore(4)`** in `enrich_snapshot_page` or the new cache-aside resolver — the 8-9s Naver tails mean Naver is already throttling; the fix reduces request count, it does not add concurrency.
- **Migration-0.** No new DB table/column, no alembic revision. The new module is a Redis cache-aside layer only. (The existing `analyst_consensus_snapshots` table is not read or written by this plan.)
- **Fail-open, hermetic.** The cache degrades to a direct live fetch whenever Redis is unavailable/disabled or returns malformed data (never raises). Guarded by `settings.analyze_fetch_cache_enabled` (default forced `false` in `tests/conftest.py:106`); no test may touch a real Redis unless it explicitly patches the client factory.
- **US path unchanged.** Every new code path is gated on `market == "kr"`; `market == "us"` continues to call the live `handle_get_investment_opinions` with no cache and no upside recompute.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/services/invest_view_model/analyst_consensus_cache.py` | Create | Task 1 — KR consensus Redis cache primitives (key, stable-field strip, get/set, TTL reuse). Task 2 — cache-aside `resolve_consensus` + per-call memo, `resolve_consensus_counts` (filter), `cached_opinion_provider` (page provider w/ fresh-price upside recompute). |
| `app/mcp_server/tooling/screener_snapshot_tool.py` | Modify | Task 3 — rewrite the `min_analyst_*` branch (`:455`) to resolve counts via cache-aside + enrich only the page; inject `cached_opinion_provider` into both `enrich_snapshot_page` calls (`:479`,`:517`). |
| `app/mcp_server/tooling/analysis_registration.py` | Modify | Task 4 — update the `screen_stocks_snapshot` tool description (`:316`) to note KR consensus is cached (daily) with intraday upside recompute. |
| `docs/runbooks/screener-analyst-consensus-cache.md` | Create | Task 4 — operator note: cache key/TTL, `ANALYZE_FETCH_CACHE_ENABLED` gate, fail-open, how to invalidate. |
| `tests/services/test_analyst_consensus_cache.py` | Create | Task 1 + Task 2 tests. |
| `tests/test_screener_snapshot_tool.py` | Modify | Task 3 tests (append min_analyst cache-aside + page-only-enrich cases). |

> **NOT touched:**
> - **`app/services/analyst_consensus_snapshots/**`, `app/models/analyst_consensus_snapshot.py`, `app/jobs/analyst_consensus_snapshots.py`, `app/tasks/analyst_consensus_snapshot_tasks.py`** — the ROB-641 precompute stack. This plan chose the Redis-cache path (holdings∪watch scope is too narrow for discovery); the precompute table is left entirely alone.
> - **`app/core/analyze_cache.py`** — reused read-only (its pure helpers `_get_redis_client`/`_fetch_cache_ttl_seconds`/`_provider_date_for_key`/`PROVIDER_NAVER` are imported, not edited) so the analyze-path cache (ROB-638) is unaffected and there is no key-namespace collision.
> - **`app/services/naver_finance/investor.py` / `parser.py`** — the live fetch functions are consumed unchanged; no per-fetch client pooling or signature change (that would be a broader refactor and a collision risk). Caching happens one layer up.
> - **US / yfinance opinions path** (`app/mcp_server/tooling/fundamentals_sources_yfinance.py`) — unchanged; US is not the bottleneck.

---

## Task 1 — KR consensus Redis cache primitives (new module, migration-0)

**Files:**
- Create `app/services/invest_view_model/analyst_consensus_cache.py`.
- Test (create) `tests/services/test_analyst_consensus_cache.py`.

**Interfaces (produced):**
- `_consensus_cache_key(market: str, symbol: str, now: datetime | None = None) -> str` → `"screener_consensus:naver:{SYMBOL}:{KST-date}"` (KR only; delegates the date to `analyze_cache._provider_date_for_key(PROVIDER_NAVER, now)`).
- `_STABLE_CONSENSUS_FIELDS: frozenset[str]` — the daily-stable keys copied into the cache: `buy_count, hold_count, sell_count, strong_buy_count, total_count, avg_target_price, median_target_price, min_target_price, max_target_price, rows_total, rows_used, rows_excluded_stale, rows_undated, newest_opinion_date, window_months, target_price_count, target_price_honest`. `current_price` and `upside_pct` are **excluded** (volatile).
- `_strip_volatile(consensus: dict[str, Any]) -> dict[str, Any]` — returns a copy with only `_STABLE_CONSENSUS_FIELDS` present (never `current_price`/`upside_pct`).
- `async def get_cached_consensus(redis_client, market: str, symbol: str) -> dict[str, Any] | None` — returns the cached stable consensus dict or `None` (miss / malformed / redis None / non-KR). Never raises.
- `async def set_cached_consensus(redis_client, market: str, symbol: str, consensus: dict[str, Any]) -> None` — caches `_strip_volatile(consensus)` with TTL `analyze_cache._fetch_cache_ttl_seconds(PROVIDER_NAVER, now_kst())`. No-op when: `redis_client is None`, `market != "kr"`, or consensus has no positive `total_count` (never cache a degraded/empty consensus). Never raises.

**Interfaces (consumed):** `app/core.analyze_cache` (`_get_redis_client`, `_fetch_cache_ttl_seconds`, `_provider_date_for_key`, `PROVIDER_NAVER`), `app/core.timezone.now_kst`, `app/core.config.settings.analyze_fetch_cache_enabled`.

Steps:

- [ ] **Write failing test — key format + volatile stripping.** Create `tests/services/test_analyst_consensus_cache.py`:
```python
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_view_model import analyst_consensus_cache as cache

pytestmark = pytest.mark.unit

_KST = ZoneInfo("Asia/Seoul")


def test_cache_key_is_kst_dated_and_namespaced():
    now = datetime(2026, 7, 4, 9, 0, tzinfo=_KST)
    assert (
        cache._consensus_cache_key("kr", "005930", now)
        == "screener_consensus:naver:005930:2026-07-04"
    )


def test_strip_volatile_drops_current_price_and_upside():
    consensus = {
        "buy_count": 2,
        "hold_count": 1,
        "sell_count": 0,
        "total_count": 3,
        "avg_target_price": 78500,
        "current_price": 69900,
        "upside_pct": 12.3,
    }
    stable = cache._strip_volatile(consensus)
    assert stable["buy_count"] == 2
    assert stable["avg_target_price"] == 78500
    assert "current_price" not in stable
    assert "upside_pct" not in stable
```

- [ ] **Run it — fails.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → ImportError (module absent).

- [ ] **Minimal impl — module scaffold + key + strip.** Create `app/services/invest_view_model/analyst_consensus_cache.py`:
```python
"""ROB-686 — KR analyst-consensus Redis cache-aside for the snapshot screener.

Caches the DAILY-STABLE analyst consensus (buy/hold/sell/total counts + target
prices) per (market, symbol, KST-date) so screen_stocks_snapshot stops re-scraping
Naver research pages (company_list/company_read) on every call. The volatile
current_price/upside_pct are stripped before caching and recomputed on the
returned page from a fresh price (see cached_opinion_provider). KR only; US
(yfinance) is not cached. Fail-open + hermetic: reuses app.core.analyze_cache's
Redis client + TTL, guarded by settings.analyze_fetch_cache_enabled.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.core import analyze_cache
from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_KEY_PREFIX = "screener_consensus"

_STABLE_CONSENSUS_FIELDS: frozenset[str] = frozenset(
    {
        "buy_count",
        "hold_count",
        "sell_count",
        "strong_buy_count",
        "total_count",
        "avg_target_price",
        "median_target_price",
        "min_target_price",
        "max_target_price",
        "rows_total",
        "rows_used",
        "rows_excluded_stale",
        "rows_undated",
        "newest_opinion_date",
        "window_months",
        "target_price_count",
        "target_price_honest",
    }
)


def _consensus_cache_key(market: str, symbol: str, now: datetime | None = None) -> str:
    date_part = analyze_cache._provider_date_for_key(analyze_cache.PROVIDER_NAVER, now)
    return f"{_KEY_PREFIX}:{analyze_cache.PROVIDER_NAVER}:{symbol.upper()}:{date_part}"


def _strip_volatile(consensus: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in consensus.items() if k in _STABLE_CONSENSUS_FIELDS}
```

- [ ] **Run it — passes.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → 2 passed.

- [ ] **Write failing test — get/set round-trip via fake redis, never-cache-degraded, fail-open.** Append:
```python
class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.mark.asyncio
async def test_set_then_get_round_trips_stable_fields_only():
    redis = _FakeRedis()
    consensus = {
        "buy_count": 2,
        "total_count": 3,
        "avg_target_price": 78500,
        "current_price": 69900,
        "upside_pct": 12.3,
    }
    await cache.set_cached_consensus(redis, "kr", "005930", consensus)
    got = await cache.get_cached_consensus(redis, "kr", "005930")
    assert got is not None
    assert got["total_count"] == 3
    assert got["avg_target_price"] == 78500
    assert "current_price" not in got  # volatile never persisted
    assert "upside_pct" not in got


@pytest.mark.asyncio
async def test_set_is_noop_for_degraded_or_us_or_no_redis():
    redis = _FakeRedis()
    # total_count 0 → degraded, never cached
    await cache.set_cached_consensus(redis, "kr", "000660", {"total_count": 0})
    assert await cache.get_cached_consensus(redis, "kr", "000660") is None
    # US never cached
    await cache.set_cached_consensus(redis, "us", "AAPL", {"total_count": 5})
    assert redis.store == {}
    # redis None → no-op, no raise
    await cache.set_cached_consensus(None, "kr", "005930", {"total_count": 3})


@pytest.mark.asyncio
async def test_get_fail_open_on_malformed_and_none():
    assert await cache.get_cached_consensus(None, "kr", "005930") is None

    class _Boom:
        async def get(self, key):
            raise RuntimeError("redis down")

    assert await cache.get_cached_consensus(_Boom(), "kr", "005930") is None
```

- [ ] **Run it — fails.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → `get_cached_consensus`/`set_cached_consensus` undefined.

- [ ] **Minimal impl — get/set with TTL reuse + guards.** Append to the module:
```python
async def get_cached_consensus(
    redis_client: Any, market: str, symbol: str
) -> dict[str, Any] | None:
    if redis_client is None or (market or "").strip().lower() != "kr":
        return None
    key = _consensus_cache_key(market, symbol)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("consensus_cache GET failed %s: %s", key, exc)
        return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def set_cached_consensus(
    redis_client: Any, market: str, symbol: str, consensus: dict[str, Any]
) -> None:
    if redis_client is None or (market or "").strip().lower() != "kr":
        return
    total = consensus.get("total_count")
    if not isinstance(total, int) or total <= 0:
        return  # never cache a degraded/empty consensus
    try:
        now = now_kst()
        ttl = analyze_cache._fetch_cache_ttl_seconds(analyze_cache.PROVIDER_NAVER, now)
        if ttl <= 0:
            return
        serialized = json.dumps(
            _strip_volatile(consensus), default=str, ensure_ascii=False
        )
        await redis_client.set(_consensus_cache_key(market, symbol, now), serialized, ex=ttl)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("consensus_cache SET failed %s: %s", symbol, exc)
```

- [ ] **Run it — passes.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → all passed.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-686): KR analyst-consensus Redis cache primitives (get/set/strip)"`

---

## Task 2 — Cache-aside resolution + filter counts + page provider (new module, migration-0)

**Files:**
- Modify `app/services/invest_view_model/analyst_consensus_cache.py` (append resolution layer).
- Test (append) `tests/services/test_analyst_consensus_cache.py`.

**Interfaces (produced):**
- `async def resolve_consensus(*, symbol: str, market: str, redis_client: Any = None, memo: dict[str, dict[str, Any] | None] | None = None, opinion_fetcher=handle_get_investment_opinions) -> dict[str, Any] | None` — cache-aside for the daily-stable consensus dict. Order: per-call `memo` → Redis (`get_cached_consensus`) → live `opinion_fetcher(symbol=..., market=..., limit=10)` (KR); on live success extract `payload["consensus"]`, `set_cached_consensus`, store stable copy in `memo`. Returns the **stable** consensus dict (no `current_price`/`upside_pct`), or `None` on failure/degraded. US: bypasses Redis, returns the live `payload["consensus"]` (memoized only). Never raises (fail-open → `None`). **`limit=10` is deliberate**: it matches the value the current min_analyst path + page enrich already use (`enrich_snapshot_page._opinion_payload` calls the provider with `limit=10` — `screener_analysis_enrichment.py:141`), so `totalCount`'s ~10 ceiling and thus `min_analyst_count` filter semantics are preserved, and cold `company_read.naver` fetches per symbol are not inflated (a higher cap would triple them, working against the ticket's goal).
- `async def resolve_consensus_counts(*, symbols: list[str], market: str, redis_client: Any = None, memo=None, concurrency: int = 4, opinion_fetcher=None) -> dict[str, dict[str, int | None]]` — resolves `{symbol: {"totalCount": int|None, "buyCount": int|None}}` for the `min_analyst_*` filter, cache-aside, under `asyncio.Semaphore(concurrency)` (default 4 — **not raised**). `opinion_fetcher` is threaded down to `resolve_consensus` for test injection (defaults to the lazily-imported `handle_get_investment_opinions`). Missing/failed symbols are simply absent from the dict; a symbol that fetched successfully but has zero coverage is present with `totalCount=0` (and is dropped by any `>= 1` filter).
- `async def cached_opinion_provider(*, symbol: str, market: str, limit: int = 10, redis_client: Any = None, memo=None, price_fetcher=_fetch_current_price) -> dict[str, Any]` — drop-in replacement for `handle_get_investment_opinions` as `enrich_snapshot_page`'s `opinion_provider`. KR: `resolve_consensus(...)`; on a stable hit, recompute `current_price`/`upside_pct` from a fresh `price_fetcher(symbol)` (fail-open: leave both absent if the price fetch fails/returns None), return `{"source": "naver", "consensus": {...}}`. On miss returns `{"error": "analyst_consensus_unavailable"}`. US: delegates to `handle_get_investment_opinions(symbol=..., market="us", limit=...)` unchanged.

**Interfaces (consumed):** `app.mcp_server.tooling.fundamentals._valuation.handle_get_investment_opinions`, `app.services.naver_finance.investor._fetch_current_price` (single `item/main.naver`), `analyze_cache._get_redis_client`.

Notes: `resolve_consensus`/`cached_opinion_provider` accept injectable `opinion_fetcher`/`price_fetcher` so tests never hit the network. Imports of `handle_get_investment_opinions` and `_fetch_current_price` are done lazily inside the functions (module-import cost + avoid import cycles), mirroring the existing lazy imports in `screener_analysis_enrichment.py`.

Steps:

- [ ] **Write failing test — cache-aside prefers cache/memo over live fetch.** Append to `tests/services/test_analyst_consensus_cache.py`:
```python
@pytest.mark.asyncio
async def test_resolve_consensus_uses_cache_and_skips_live_fetch():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis, "kr", "005930", {"buy_count": 2, "total_count": 3, "avg_target_price": 78500}
    )
    calls: list[str] = []

    async def _live(*, symbol, market, limit):
        calls.append(symbol)
        return {"consensus": {"total_count": 99}}

    got = await cache.resolve_consensus(
        symbol="005930", market="kr", redis_client=redis, opinion_fetcher=_live
    )
    assert got is not None and got["total_count"] == 3  # from cache, not live 99
    assert calls == []  # live fetcher never called


@pytest.mark.asyncio
async def test_resolve_consensus_populates_cache_and_memo_on_miss():
    redis = _FakeRedis()
    memo: dict = {}
    calls: list[str] = []

    async def _live(*, symbol, market, limit):
        calls.append(symbol)
        return {
            "consensus": {
                "buy_count": 1,
                "total_count": 2,
                "avg_target_price": 100,
                "current_price": 90,
                "upside_pct": 11.1,
            }
        }

    got = await cache.resolve_consensus(
        symbol="000660", market="kr", redis_client=redis, memo=memo, opinion_fetcher=_live
    )
    assert got["total_count"] == 2 and "current_price" not in got  # stable only
    assert (await cache.get_cached_consensus(redis, "kr", "000660"))["total_count"] == 2
    # second resolve is served from memo — live fetcher not called again
    await cache.resolve_consensus(
        symbol="000660", market="kr", redis_client=redis, memo=memo, opinion_fetcher=_live
    )
    assert calls == ["000660"]
```

- [ ] **Run it — fails.** `resolve_consensus` undefined.

- [ ] **Minimal impl — `resolve_consensus`.** Append:
```python
async def resolve_consensus(
    *,
    symbol: str,
    market: str,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    opinion_fetcher: Any = None,
) -> dict[str, Any] | None:
    market_norm = (market or "").strip().lower()
    memo_key = f"{market_norm}:{symbol.upper()}"
    if memo is not None and memo_key in memo:
        return memo[memo_key]

    if opinion_fetcher is None:
        from app.mcp_server.tooling.fundamentals._valuation import (
            handle_get_investment_opinions,
        )

        opinion_fetcher = handle_get_investment_opinions

    if market_norm == "kr":
        cached = await get_cached_consensus(redis_client, market_norm, symbol)
        if cached is not None:
            if memo is not None:
                memo[memo_key] = cached
            return cached

    stable: dict[str, Any] | None = None
    try:
        # limit=10 preserves the existing filter/page ceiling (see interface note);
        # do NOT bump this — a higher cap triples cold company_read.naver fetches.
        payload = await opinion_fetcher(symbol=symbol, market=market_norm, limit=10)
        consensus = (payload or {}).get("consensus") if isinstance(payload, dict) else None
        if isinstance(consensus, dict) and isinstance(consensus.get("total_count"), int):
            if market_norm == "kr":
                await set_cached_consensus(redis_client, market_norm, symbol, consensus)
                stable = _strip_volatile(consensus)
            else:
                stable = consensus  # US: not cached, returned as-is
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("resolve_consensus live fetch failed %s: %s", symbol, exc)
        stable = None

    if memo is not None:
        memo[memo_key] = stable
    return stable
```

- [ ] **Run it — passes.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → all passed.

- [ ] **Write failing test — counts resolver + page provider upside recompute.** Append:
```python
@pytest.mark.asyncio
async def test_resolve_consensus_counts_maps_symbols():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis, "kr", "005930", {"buy_count": 2, "total_count": 3}
    )

    async def _live(*, symbol, market, limit):
        return {"consensus": {"buy_count": 5, "total_count": 7}}

    memo: dict = {}
    counts = await cache.resolve_consensus_counts(
        symbols=["005930", "000660"], market="kr", redis_client=redis, memo=memo,
        opinion_fetcher=_live,
    )
    assert counts["005930"] == {"totalCount": 3, "buyCount": 2}   # cache
    assert counts["000660"] == {"totalCount": 7, "buyCount": 5}   # live


@pytest.mark.asyncio
async def test_cached_opinion_provider_recomputes_upside_from_fresh_price():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis, "kr", "005930",
        {"buy_count": 2, "hold_count": 1, "sell_count": 0, "total_count": 3,
         "avg_target_price": 110},
    )

    async def _price(code):
        return 100  # fresh price → upside = (110-100)/100*100 = 10.0

    payload = await cache.cached_opinion_provider(
        symbol="005930", market="kr", redis_client=redis, price_fetcher=_price,
    )
    c = payload["consensus"]
    assert c["current_price"] == 100
    assert c["upside_pct"] == pytest.approx(10.0)
    assert c["total_count"] == 3  # daily-stable count preserved


@pytest.mark.asyncio
async def test_cached_opinion_provider_fail_open_when_price_missing():
    redis = _FakeRedis()
    await cache.set_cached_consensus(
        redis, "kr", "005930", {"total_count": 3, "avg_target_price": 110},
    )

    async def _price(code):
        return None  # price unavailable → no stale upside served

    payload = await cache.cached_opinion_provider(
        symbol="005930", market="kr", redis_client=redis, price_fetcher=_price,
    )
    c = payload["consensus"]
    assert c.get("upside_pct") is None
    assert c.get("current_price") is None
```

- [ ] **Run it — fails.** `resolve_consensus_counts` / `cached_opinion_provider` undefined.

- [ ] **Minimal impl — counts resolver + page provider.** Append:
```python
import asyncio


async def resolve_consensus_counts(
    *,
    symbols: list[str],
    market: str,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    concurrency: int = 4,
    opinion_fetcher: Any = None,
) -> dict[str, dict[str, int | None]]:
    sem = asyncio.Semaphore(max(1, concurrency))  # NOT raised — Naver is throttling
    out: dict[str, dict[str, int | None]] = {}

    async def _one(symbol: str) -> None:
        async with sem:
            stable = await resolve_consensus(
                symbol=symbol, market=market, redis_client=redis_client,
                memo=memo, opinion_fetcher=opinion_fetcher,
            )
            if stable is not None:
                out[symbol] = {
                    "totalCount": stable.get("total_count"),
                    "buyCount": stable.get("buy_count"),
                }

    await asyncio.gather(*(_one(s) for s in dict.fromkeys(symbols)))
    return out


def _recompute_upside(consensus: dict[str, Any], price: float | int | None) -> dict[str, Any]:
    out = dict(consensus)
    avg = out.get("avg_target_price")
    if price and isinstance(price, (int, float)) and avg and isinstance(avg, (int, float)):
        out["current_price"] = price
        out["upside_pct"] = round((avg - price) / price * 100, 2)
    else:
        out.pop("current_price", None)
        out.pop("upside_pct", None)
    return out


async def cached_opinion_provider(
    *,
    symbol: str,
    market: str,
    limit: int = 10,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    price_fetcher: Any = None,
    opinion_fetcher: Any = None,
) -> dict[str, Any]:
    market_norm = (market or "").strip().lower()
    if market_norm != "kr":
        from app.mcp_server.tooling.fundamentals._valuation import (
            handle_get_investment_opinions,
        )

        return await handle_get_investment_opinions(
            symbol=symbol, market=market_norm, limit=limit
        )

    stable = await resolve_consensus(
        symbol=symbol, market=market_norm, redis_client=redis_client,
        memo=memo, opinion_fetcher=opinion_fetcher,
    )
    if stable is None:
        return {"error": "analyst_consensus_unavailable"}

    if price_fetcher is None:
        from app.services.naver_finance.investor import _fetch_current_price

        price_fetcher = _fetch_current_price
    try:
        price = await price_fetcher(symbol)
    except Exception:  # noqa: BLE001 — fail-open, no stale upside
        price = None

    return {"source": "naver", "consensus": _recompute_upside(stable, price)}
```

- [ ] **Run it — passes.** `uv run pytest tests/services/test_analyst_consensus_cache.py -v` → all passed.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-686): KR consensus cache-aside resolve/counts/page-provider with fresh-price upside"`

---

## Task 3 — Rewire the screener `min_analyst_*` branch (fix pre-pagination over-enrichment, migration-0)

**Files:**
- Modify `app/mcp_server/tooling/screener_snapshot_tool.py` — the `min_analyst_*` branch (`:455`–`:497`) and both `enrich_snapshot_page` calls (`:479`,`:517`).
- Test (append) `tests/test_screener_snapshot_tool.py`.

**Interfaces (consumed):** `analyst_consensus_cache.resolve_consensus_counts`, `analyst_consensus_cache.cached_opinion_provider`, `analyze_cache._get_redis_client`.

**Current behavior to replace (`:455`–`:497`):** when `min_analyst_*` is set, the tool hard-errors above 200 rows, then live-`enrich_snapshot_page`s the **whole** matched set, filters on the enriched `analysisContext.consensus`, and paginates. The no-filter branch (`:512`–`:523`) already enriches only the page.

**Target behavior:**
1. Build a per-call `memo: dict = {}` and `redis_client = await analyze_cache._get_redis_client()` once.
2. `min_analyst_*` branch: keep the `_MAX_ANALYST_ENRICHMENT_ROWS` cold-path guard (now a *resolution* bound), then `counts = await resolve_consensus_counts(symbols=[all matched symbols], market=market, redis_client=redis_client, memo=memo)`; filter `all_results` by `counts[symbol]["totalCount"] >= min_analyst_count` and/or `["buyCount"] >= min_analyst_buy_count` (drop symbols absent from `counts`); recompute `total_available`; paginate; then `enrich_snapshot_page(rows=page, market=market, session_factory=_session_factory(), opinion_provider=functools.partial(cached_opinion_provider, redis_client=redis_client, memo=memo))`.
3. No-filter branch (`:512`): same `opinion_provider=partial(cached_opinion_provider, redis_client=redis_client, memo=memo)` injection so the KR page consensus is cached too.
4. `_consensus_count` (`:166`) is no longer used by the filter (it read enriched rows). Remove it if unused after the rewrite, else leave a single caller. Verify with `grep -n _consensus_count app/mcp_server/tooling/screener_snapshot_tool.py`.

Steps:

- [ ] **Write failing test — min_analyst path filters via cache-aside counts and enriches only the page.** Append to `tests/test_screener_snapshot_tool.py` (reuse `_patch_build_with_n_results` / `_FakeCM` already in the file):
```python
@pytest.mark.unit
@pytest.mark.asyncio
async def test_min_analyst_filters_via_counts_and_enriches_only_page(monkeypatch) -> None:
    _patch_build_with_n_results(monkeypatch, 5)  # symbols S0..S4

    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        # S0,S1,S2 qualify (>=3), S3,S4 do not
        return {
            s: {"totalCount": (3 if i < 3 else 1), "buyCount": (2 if i < 3 else 0)}
            for i, s in enumerate(symbols)
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )

    enriched_symbols: list[list[str]] = []

    async def _fake_enrich_page(*, rows, market, session_factory, opinion_provider=None):
        enriched_symbols.append([r["symbol"] for r in rows])
        return {
            "results": [{**r, "analystLabel": "x", "analysisContext": {}} for r in rows],
            "summary": {"attempted": len(rows), "warnings": []},
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.screener_analysis_enrichment.enrich_snapshot_page",
        _fake_enrich_page,
    )

    out = await tool.screen_stocks_snapshot_impl(
        preset="consecutive_gainers", market="kr", min_analyst_count=3, limit=2, offset=0
    )
    # 3 qualified, page of 2
    assert out["pagination"]["total_available"] == 3
    assert len(out["results"]) == 2
    # enrichment saw ONLY the 2 returned page rows, not all matched/qualified rows
    assert enriched_symbols == [["S0", "S1"]]
```

- [ ] **Run it — fails.** Current impl enriches all 5 rows before filtering (asserts on `enriched_symbols == [["S0","S1"]]` and `total_available == 3` fail).

- [ ] **Minimal impl — rewrite the branch.** In `app/mcp_server/tooling/screener_snapshot_tool.py`:
  - Add near the other imports: `import functools`.
  - Before the pagination block (`:448`), add `memo: dict[str, Any] = {}` and, lazily inside the enrichment sections, `from app.services.invest_view_model import analyst_consensus_cache` + `from app.core import analyze_cache`; resolve `redis_client = await analyze_cache._get_redis_client()` once.
  - Replace the `min_analyst_*` branch (`:455`–`:497`) with:
```python
    if min_analyst_count is not None or min_analyst_buy_count is not None:
        if len(all_results) > _MAX_ANALYST_ENRICHMENT_ROWS:
            return {
                "error": (
                    "analyst enrichment row cap exceeded; narrow presets, "
                    "market-cap filters, or exclude_symbols before applying analyst filters"
                ),
                "preset": preset,
                "presets": preset_ids,
                "results": [],
                "pagination": {
                    "total_available": len(all_results),
                    "returned_count": 0,
                    "offset": eff_offset,
                    "limit": eff_limit,
                    "has_more": False,
                    "next_offset": None,
                },
            }

        from app.core import analyze_cache
        from app.services.invest_view_model import analyst_consensus_cache

        redis_client = await analyze_cache._get_redis_client()
        matched_symbols = [
            str(r.get("symbol") or "").strip()
            for r in all_results
            if r.get("symbol")
        ]
        counts = await analyst_consensus_cache.resolve_consensus_counts(
            symbols=matched_symbols,
            market=market,
            redis_client=redis_client,
            memo=memo,
        )

        def _passes(row: dict[str, Any]) -> bool:
            c = counts.get(str(row.get("symbol") or "").strip())
            if c is None:
                return False
            if min_analyst_count is not None and (c.get("totalCount") or 0) < int(
                min_analyst_count
            ):
                return False
            if min_analyst_buy_count is not None and (c.get("buyCount") or 0) < int(
                min_analyst_buy_count
            ):
                return False
            return True

        all_results = [r for r in all_results if _passes(r)]
        total_available = len(all_results)
        page = all_results[eff_offset : eff_offset + eff_limit]

        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        enrichment = await enrich_snapshot_page(
            rows=page,
            market=market,
            session_factory=_session_factory(),
            opinion_provider=functools.partial(
                analyst_consensus_cache.cached_opinion_provider,
                redis_client=redis_client,
                memo=memo,
            ),
        )
        page = enrichment["results"]
        payload["analysisEnrichment"] = enrichment["summary"]
    else:
        page = all_results[eff_offset : eff_offset + eff_limit]
```
  - Replace the no-filter enrichment block (`:512`–`:523`) so it injects the cached provider:
```python
    if min_analyst_count is None and min_analyst_buy_count is None:
        from app.core import analyze_cache
        from app.services.invest_view_model import analyst_consensus_cache
        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        redis_client = await analyze_cache._get_redis_client()
        enrichment = await enrich_snapshot_page(
            rows=page,
            market=market,
            session_factory=_session_factory(),
            opinion_provider=functools.partial(
                analyst_consensus_cache.cached_opinion_provider,
                redis_client=redis_client,
                memo=memo,
            ),
        )
        payload["results"] = enrichment["results"]
        payload["analysisEnrichment"] = enrichment["summary"]
```
  - Remove `_consensus_count` (`:166`) if now unused (confirm via grep).

- [ ] **Run it — passes.** `uv run pytest tests/test_screener_snapshot_tool.py -k "min_analyst_filters_via_counts" -v` → passes.

- [ ] **REQUIRED — rewrite the existing `test_snapshot_tool_filters_market_cap_and_analyst` (`tests/test_screener_snapshot_tool.py:811`).** This is the pre-existing coverage for the `min_analyst_*` filter and it **WILL BREAK** under the rewrite: today it filters on the enriched `analysisContext.consensus` produced by a monkeypatched `enrich_snapshot_page`; the new impl filters on `resolve_consensus_counts` (which this test does **not** stub), so its two analyst sub-assertions (`min_analyst_buy_count=1` at `:867` and `min_analyst_count=1` at `:879`) would fall through to a real live `handle_get_investment_opinions` network fetch (fail-open → empty counts → both symbols dropped → assertions fail). Split it: keep the two market-cap sub-cases (no `min_analyst_*`) on the existing `_fake_enrich_page` (add `**kwargs`/`opinion_provider=None` — it already takes `**kwargs`), and for the analyst sub-cases monkeypatch `app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts` to return the S1/S2 counts directly, e.g.:
```python
    async def _fake_counts(*, symbols, market, redis_client=None, memo=None, **kw):
        return {
            "S1": {"totalCount": 2, "buyCount": 2},
            "S2": {"totalCount": 1, "buyCount": 0},
        }

    monkeypatch.setattr(
        "app.services.invest_view_model.analyst_consensus_cache.resolve_consensus_counts",
        _fake_counts,
    )
```
  Then `min_analyst_buy_count=1` → `["S1"]`, `min_analyst_count=1` → `["S1", "S2"]` (unchanged expectations), but now sourced from the cache-aside counts instead of enriched rows. The page-only `enrich_snapshot_page` fake for these sub-cases must accept `opinion_provider=None`.

- [ ] **Adjust the remaining `_fake_enrich_page` signatures.** In `tests/test_screener_snapshot_tool.py`, every monkeypatched `enrich_snapshot_page` fake must accept the now-always-injected `opinion_provider` kwarg. Update `test_enriches_only_returned_page`'s `_fake_enrich_page(*, rows, market, session_factory)` → add `opinion_provider=None` (its no-filter branch now passes the cached provider partial). `test_snapshot_tool_filters_market_cap_and_analyst`'s fake already uses `**kwargs` so it absorbs it. `test_snapshot_tool_analyst_filter_rejects_large_unpaged_enrichment` (`:986`, 201 rows) still passes untouched — the `_MAX_ANALYST_ENRICHMENT_ROWS` guard returns the row-cap error before `resolve_consensus_counts` is ever reached.

- [ ] **Regression — full tool + enrichment suites.** `uv run pytest tests/test_screener_snapshot_tool.py tests/services/test_screener_analysis_enrichment.py -v` → all pass. (`test_screener_analysis_enrichment.py` exercises `enrich_snapshot_page` directly with its own `opinion_provider`; its signature is unchanged by this plan, so it stays green.)

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "fix(ROB-686): screener min_analyst filters via cached counts, enrich only the page"`

---

## Task 4 — Tool description + operator runbook (doc-only, migration-0)

**Files:**
- Modify `app/mcp_server/tooling/analysis_registration.py` — `screen_stocks_snapshot` description (`:316`–`:338`).
- Create `docs/runbooks/screener-analyst-consensus-cache.md`.

**Interfaces:** no signature change; `register_analysis_tools(mcp)` unchanged.

Steps:

- [ ] **Write failing test — description mentions the cache + intraday upside recompute.** Append to `tests/test_screener_snapshot_tool.py` (or a small new `tests/mcp_server/tooling/test_screen_stocks_snapshot_docs.py` mirroring the ROB-669 `_FakeMCP` doc-test pattern):
```python
def test_screen_stocks_snapshot_description_notes_consensus_cache():
    from app.mcp_server.tooling.analysis_registration import register_analysis_tools

    class _FakeMCP:
        def __init__(self):
            self.descriptions = {}

        def tool(self, *, name, description, **kw):
            def _d(fn):
                self.descriptions[name] = description
                return fn
            return _d

    mcp = _FakeMCP()
    register_analysis_tools(mcp)
    desc = mcp.descriptions["screen_stocks_snapshot"].lower()
    assert "consensus" in desc
    assert "cache" in desc or "cached" in desc
```
  (Confirm `register_analysis_tools` is the real registrar name via `grep -n "def register" app/mcp_server/tooling/analysis_registration.py` and that `_FakeMCP.tool` accepts the same kwargs the real registration uses.)

- [ ] **Run it — fails.** Current description names no cache.

- [ ] **Minimal impl — extend the description.** Append one sentence to the `screen_stocks_snapshot` description string (`:316`), e.g.:
  `"KR analyst consensus (buy/hold/sell counts + target prices) is cached daily per symbol (Redis, KST-date TTL); the displayed target-upside is recomputed each call from a fresh price so it stays intraday-current. min_analyst_* filters resolve consensus from the cache and only the returned page is enriched."`

- [ ] **Run it — passes.**

- [ ] **Create the runbook.** `docs/runbooks/screener-analyst-consensus-cache.md`: cache key format (`screener_consensus:naver:{SYMBOL}:{KST-date}`), TTL (session-close 15:35 KST, then next KST midnight — same as ROB-638 `analyze_cache`), the `ANALYZE_FETCH_CACHE_ENABLED` gate (shared with the analyze path; `false` disables → live fetch every call), fail-open behavior, KR-only scope (US uncached), and how to invalidate (`redis-cli --scan --pattern "screener_consensus:*"` then `DEL`). Note migration-0 and that the ROB-641 `analyst_consensus_snapshots` precompute table is a separate, untouched surface.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "docs(ROB-686): note screener consensus cache in tool description + runbook"`

---

## Verification (whole plan)

- [ ] `uv run pytest tests/services/test_analyst_consensus_cache.py tests/test_screener_snapshot_tool.py tests/services/test_screener_analysis_enrichment.py -v` → all green.
- [ ] `make lint` clean.
- [ ] Sanity: with `ANALYZE_FETCH_CACHE_ENABLED=false` (test default) every path falls open to live fetch and existing behavior is preserved; with it enabled, a second `screen_stocks_snapshot` call for the same KR symbols in the same KST day issues **no** `company_list.naver`/`company_read.naver` (only the per-page `item/main.naver` price recompute). Confirm via the resolver/provider unit tests (network-injected), not a live call.
