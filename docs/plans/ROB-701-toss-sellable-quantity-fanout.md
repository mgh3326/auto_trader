# ROB-701 — Toss `sellable_quantity` N+1 Fanout on `/invest` Home (Short-TTL Cache) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Kill the dominant `/invest/api/home` bottleneck — the `invest.home.toss_api` reader spends ~15–17s serially fanning out one `GET /api/v1/sellable-quantity` per holding (Sentry 24h: 2659 calls, sum 344s). Root cause: `fetch_toss_portfolio_snapshot` (`app/services/toss_portfolio_service.py:131`) `asyncio.gather`s `sellable_quantity(symbol=...)` per position (`:155-161`), but that call is in the Toss `ORDER_INFO` rate-limit group capped at **6 TPS** (3 TPS in the 09:00–09:10 KST peak) (`app/services/brokers/toss/rate_limiter.py:35,52-55`), so the fanout serializes to ~6/sec → N positions ≈ N/6 s (30≈5s, 60≈10s). ROB-685 added a `need_sellable=False` skip (`:173-177`), but the `/invest` home reader passes `need_sellable=mutations_enabled` (`app/services/invest_home_readers.py:554-556`) and prod runs `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true` (ROB-549 sellable-accuracy gate), so the fanout runs on **every** load. We add a **process-global short-TTL (default 45s) per-symbol sellable cache** and thread it into `fetch_toss_portfolio_snapshot` as an opt-in parameter. The invest_home reader (the sole hot caller with mutations armed) passes the shared cache: the first load populates it (still one bounded fanout), and every subsequent `/invest` home / account-panel load within the TTL reuses cached values → **0** `sellable-quantity` calls. A short TTL naturally refreshes after order fills, and ROB-549 tolerates that brief staleness (the actual sell path re-validates at the broker, unchanged). The MCP collect chain (`portfolio_holdings.py:559`) passes **no** cache and stays byte-for-byte identical; the ROB-685 `need_sellable=False` fast path is untouched.

## Architecture

### Current (slow) flow — real refs

- `TossApiHomeReader.fetch` (`app/services/invest_home_readers.py:539`) reads `mutations_enabled = settings.toss_live_order_mutations_enabled` (`:547-549`) and calls `fetch_toss_portfolio_snapshot(need_sellable=mutations_enabled)` (`:554-556`). In prod `mutations_enabled` is `True`.
- `fetch_toss_portfolio_snapshot(*, need_sellable=True, client=None)` (`toss_portfolio_service.py:131`):
  - `holdings = await active_client.holdings()` (`:144`).
  - When `need_sellable`: opens the `invest.home.toss_api.sellable_quantity` span and `asyncio.gather`s `active_client.sellable_quantity(symbol=item.symbol)` **for every holding** (`:149-169`), then `zip`s into `paired` (`:170-172`).
  - When `not need_sellable` (ROB-685): `paired = [(item, None) for item in holdings.items]` (`:173-177`) — no fanout.
  - The position-build loop (`:179-213`) unwraps `sellable_result.sellable_quantity` (`:191-192`) into `TossPortfolioPosition.sellable_quantity`.
- Each `sellable_quantity` call → `TossReadClient.sellable_quantity(*, symbol)` (`app/services/brokers/toss/client.py:310-319`), `group=TossApiGroup.ORDER_INFO`.
- The shared `TossRateLimiter.acquire` (`rate_limiter.py:57-73`) admits `_BASE_LIMITS[ORDER_INFO]=6` per sliding second (`:35`), dropping to `3` in 09:00–09:10 (`:52-55`). The limiter is a process-global singleton (`get_shared_rate_limiter`, `:79-85`) shared by every `from_settings` client (`client.py:72`), so N gathered calls serialize to ~N/6 s of pure wall time.
- Downstream: `_toss_sellable_quantity(position, mutations_enabled)` (`invest_home_readers.py:516-524`) returns `float(position.sellable_quantity)` (falling back to full quantity) when mutations are armed → `Holding.sellableQuantity` / `pendingSellQuantity` (`:657-663`).

**Why not Option 1 (reuse a holdings field):** `TossHoldingItem` (`app/services/brokers/toss/dto.py:64-77`) carries `symbol/name/marketCountry/currency/quantity/lastPrice/averagePurchasePrice/marketValue/profitLoss/dailyProfitLoss/cost` — **no** sellable/orderable/tradable field — and `parse_holdings` (`:154-175`) only reads those keys (`raw_overview` at `:174` captures portfolio-level top keys, not per-item extras). Toss deliberately exposes a **separate** `GET /api/v1/sellable-quantity` endpoint precisely because *sellable* differs from *held* quantity (pending sells / unsettled state), so `item.quantity` is **not** a safe substitute. Option 1 is unavailable. (Recorded in key_decisions.)

### Target (cached) flow

New process-global `TossSellableCache` (new module `app/services/toss_sellable_cache.py`): a per-symbol TTL map with an **injected clock** (`now: Callable[[], float] = time.monotonic`), keyed by symbol, storing the `Decimal` value with an expiry. `get(symbol)` returns the value only while fresh (evicts on expiry); `put(symbol, value)` stamps `now() + ttl`. Module singleton `get_shared_sellable_cache()` / `reset_shared_sellable_cache()` mirrors the existing `get_shared_rate_limiter` / `reset_shared_rate_limiter` pattern (`rate_limiter.py:76-91`). A `toss_sellable_cache_enabled=False` kill-switch makes `get()`/`put()` no-ops (cache always misses → today's fanout-every-load behavior).

`fetch_toss_portfolio_snapshot(*, need_sellable=True, sellable_cache: TossSellableCache | None = None, client=None)`:

```
if need_sellable and sellable_cache is not None:      # ROB-701 cache-aware fanout
    hits = [sellable_cache.get(item.symbol) for item in holdings.items]   # index-aligned Decimal|None
    miss_indices = [i for i, h in enumerate(hits) if h is None]
    with span("invest.home.toss_api.sellable_quantity"):
        fetched = await asyncio.gather(*[client.sellable_quantity(symbol=items[i].symbol) for i in miss_indices], return_exceptions=True)
    for i, result in zip(miss_indices, fetched):
        if not isinstance(result, BaseException):
            sellable_cache.put(items[i].symbol, result.sellable_quantity)   # only SUCCESS cached
    paired = per-item: fetched result for misses; TossSellableQuantity(hits[i]) re-wrap for hits
elif need_sellable:                                    # ROB-685 default fanout (VERBATIM)
    <today's gather + span + zip>
else:                                                  # ROB-685 skip (VERBATIM)
    paired = [(item, None) for item in holdings.items]
```

The position-build loop (`:179-213`) is **unchanged** — cache hits are re-wrapped into `TossSellableQuantity(sellable_quantity=cached)` so the downstream `.sellable_quantity` unwrap is uniform. `TossApiHomeReader.fetch` passes `sellable_cache=get_shared_sellable_cache()` when `mutations_enabled` (else `None`, which lands on the `need_sellable=False` skip anyway). Net: first `/invest` home load pays one bounded fanout (miss on every symbol); loads within the TTL reuse → 0 `sellable-quantity` calls; the 6-TPS cap is never touched, `sellable_quantity()` semantics/group are untouched, and the MCP path keeps the uncached default.

## Tech Stack

Python 3.13, uv, pytest + pytest-asyncio (`@pytest.mark.asyncio`, markers `unit`/`asyncio`), asyncio, dataclass DTOs (`TossSellableQuantity`), Sentry spans, stdlib `time.monotonic`, pydantic-settings `Settings` (`app/core/config.py:184`). No new dependency, **no Redis** (in-process singleton, matching `get_shared_rate_limiter`), **migration-0** (no DB change). Toss Open API `GET /api/v1/sellable-quantity` (`TossApiGroup.ORDER_INFO`, 6 TPS / 3 TPS peak) — call frequency reduced, never rate raised.

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **Do NOT regress ROB-549 sellable/tradeable display accuracy** — sellable shown when mutations armed must remain correct (a short cache TTL staleness window is acceptable; silently dropping accuracy is not).
- **Preserve the existing `need_sellable=False` fast path (ROB-685) unchanged.**
- **No order/mutation path change. No change to the `sellable_quantity()` semantics or its rate-limit group.**
- **migration-0.**
- **TDD:** cache-hit => 0 sellable-quantity calls (or Option-1: 0 calls at all); TTL expiry => refetch; accuracy preserved (the sellable value on the Holding matches source); the mutations-disabled skip path still skips.
- **Deterministic tests:** inject/fake the Toss client + clock; assert call counts; no real network.
- Run tests with `uv run pytest <path> -v`. Lint with `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

## Approach / decision note

- **Option 2 (short-TTL per-symbol cache) chosen.** Option 1 (reuse a holdings-item sellable field) is unavailable — the holdings item has no sellable/orderable field and Toss splits it into a separate endpoint on purpose (`dto.py:64-77`). Option 3 (defer/lazy-load) is heavier UI surgery and not needed once the cache collapses repeated loads.
- **Symbol-only cache key is safe because Toss is a single operator account, not per-user.** `TossApiHomeReader.fetch` discards its `user_id` (`invest_home_readers.py:540` `del user_id`) and `fetch_toss_portfolio_snapshot` always resolves `TossReadClient.from_settings()` (one `toss_api_account_seq`/`toss_api_client_id` from `Settings`, `client.py:68`), so there is exactly one account's sellable quantity per symbol process-wide — a `(user_id, symbol)` key is unnecessary and a `symbol` key cannot leak across accounts. **If Toss ever becomes multi-account, this cache key MUST be re-scoped.** (Recorded in key_decisions.)
- **Cache is opt-in, threaded via a parameter — NOT read from a global flag inside the shared fetch, and NOT applied to the MCP path.** Only the invest_home reader passes the cache. ROB-685's approach note cautioned against a stateful cache *on the sell-sizing path* (MCP `_toss_api_position_to_mcp` / `sellable_quantity > 0 → sell_review`); scoping the cache to the display-only home reader sidesteps that entirely — the MCP collect chain (`portfolio_holdings.py:559`) still gets fresh values every call. (Recorded in key_decisions.)
- **TTL-bounded staleness is the accepted tradeoff for ROB-549.** The home reader's `sellableQuantity`/`pendingSellQuantity` are display fields; a ≤45s stale value never sizes a real order (the Toss sell tools re-validate sellable at submit, unchanged). Only **successful** fetches are cached — a transient `sellable_quantity` error is never cached, so the next load retries. (Recorded in open_questions for reviewer sign-off on the exact TTL.)
- **Both `/invest/api/home` and `/invest/api/account-panel` are served by `TossApiHomeReader.fetch`** (the single `fetch_toss_portfolio_snapshot` call site in `invest_home_readers.py` is at `:554`), so wiring the shared cache there fixes both surfaces with one change; the process-global singleton also lets the two surfaces share warm entries.

## File Structure

| File | Create/Modify | Responsibility (Task) |
|------|---------------|------------------------|
| `app/core/config.py` | Modify | Task 1 — add `toss_sellable_cache_enabled: bool = True` + `toss_sellable_cache_ttl_seconds: float = 45.0` to `Settings`, after `toss_live_order_mutations_enabled` (`:248`). |
| `app/services/toss_sellable_cache.py` | Create | Task 2 — `TossSellableCache` (injected clock, per-symbol TTL, enable flag) + `get_shared_sellable_cache` / `reset_shared_sellable_cache` singleton. |
| `app/services/toss_portfolio_service.py` | Modify | Task 3 — add `sellable_cache` param to `fetch_toss_portfolio_snapshot`; cache-aware fanout (miss-only) when provided; default + `need_sellable=False` paths unchanged. |
| `app/services/invest_home_readers.py` | Modify | Task 4 — `TossApiHomeReader.fetch` passes `sellable_cache=get_shared_sellable_cache()` when mutations armed. |
| `tests/test_toss_sellable_cache.py` | Create | Task 2 tests — TTL hit/expiry, disabled no-op, singleton identity/reset (fake clock). |
| `tests/test_toss_portfolio_service.py` | Modify | Task 3 tests — cache-hit=0 calls, TTL expiry refetch, accuracy from cache, error-not-cached, default/`need_sellable=False` unchanged (fake client + fake clock). |
| `tests/test_invest_home_readers.py` | Modify | Task 4 tests — reader passes a cache when mutations on / `None` when off; update the 4 existing Toss-reader fakes to accept the new kwarg. |

> **NOT touched:**
> - `app/services/brokers/toss/rate_limiter.py` and `.../client.py` — the 6-TPS `ORDER_INFO` cap and the `sellable_quantity` GET are unchanged; we reduce *how often* it is called, never *how fast*, and never change its rate-limit group.
> - `app/services/brokers/toss/dto.py` — `TossHoldingItem` / `parse_holdings` / `TossSellableQuantity` are read as-is (Task 3 only *imports* `TossSellableQuantity` to re-wrap cache hits).
> - `app/mcp_server/tooling/portfolio_holdings.py` (`_collect_toss_api_positions` → `fetch_toss_portfolio_snapshot(need_sellable=need_sellable)` at `:559`) and every MCP / action_report / sell-classification consumer — they pass **no** `sellable_cache`, so they keep the default uncached behavior byte-for-byte; sell classification (`sellable_quantity > 0`) still reads fresh values.
> - The ROB-685 `need_sellable=False` branch (`toss_portfolio_service.py:173-177`) and the default `need_sellable=True` **no-cache** branch (`:149-172`) — both verbatim.
> - Any order/mutation path (Toss preview/place/modify/cancel) — the cache is on a read-only GET only. No DB migration.

---

## Task 1 — Config flags for the Toss sellable cache (migration-0)

**Files:**
- Modify `app/core/config.py` — add two fields to `Settings` (`:184`) directly after `toss_live_order_mutations_enabled` (`:248`), before the `# ROB-576` block (`:250`).
- Test (create) `tests/test_toss_sellable_cache.py` (config assertions live in the same new module used by Task 2; add the config test class now).

**Interfaces:**
- Produces `Settings.toss_sellable_cache_enabled: bool = True` and `Settings.toss_sellable_cache_ttl_seconds: float = 45.0` (mirrors the existing `bool` + `float` field style near `:248`).

Steps:

- [ ] **Write the failing test — defaults present and typed.** Create `tests/test_toss_sellable_cache.py` with this first block:
```python
from __future__ import annotations

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.unit


class TestTossSellableCacheSettings:
    def test_defaults(self):
        s = Settings()
        assert s.toss_sellable_cache_enabled is True
        assert s.toss_sellable_cache_ttl_seconds == 45.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_ENABLED", "false")
        monkeypatch.setenv("TOSS_SELLABLE_CACHE_TTL_SECONDS", "30")
        s = Settings()
        assert s.toss_sellable_cache_enabled is False
        assert s.toss_sellable_cache_ttl_seconds == 30.0
```

- [ ] **Run it — fails.** `uv run pytest tests/test_toss_sellable_cache.py -k TossSellableCacheSettings -v`
  Expected: `AttributeError` — the two fields do not exist on `Settings` yet. (Confirm no clash: `grep -n "sellable_cache" app/core/config.py` returns nothing today.)

- [ ] **Minimal impl — add the fields.** In `app/core/config.py`, immediately after line 248 (`toss_live_order_mutations_enabled: bool = False`), insert:
```python

    # ROB-701: process-global short-TTL cache for the per-symbol Toss
    # sellable-quantity fanout on /invest home & account-panel. Collapses
    # repeated loads to 0 ORDER_INFO (6 TPS) calls within the TTL; a fill
    # naturally refreshes after ≤ttl (ROB-549 tolerates brief staleness).
    # enabled=False => cache always misses => today's fanout-every-load.
    toss_sellable_cache_enabled: bool = True
    toss_sellable_cache_ttl_seconds: float = 45.0
```

- [ ] **Run it — passes.** `uv run pytest tests/test_toss_sellable_cache.py -k TossSellableCacheSettings -v` → 2 passed.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-701): add toss_sellable_cache_{enabled,ttl_seconds} settings"`

---

## Task 2 — `TossSellableCache` + module singleton (migration-0)

**Files:**
- Create `app/services/toss_sellable_cache.py`.
- Test (extend) `tests/test_toss_sellable_cache.py`.

**Interfaces:**
- `class TossSellableCache` — `__init__(self, *, ttl_seconds: float, now: Callable[[], float] = time.monotonic, enabled: bool = True)`. Methods: `get(symbol: str) -> Decimal | None` (fresh value or `None`; evicts on expiry; always `None` when disabled), `put(symbol: str, value: Decimal) -> None` (stamps `now() + ttl`; no-op when disabled), `clear() -> None`.
- Module singleton: `def get_shared_sellable_cache() -> TossSellableCache` (built once from `settings.toss_sellable_cache_ttl_seconds` / `..._enabled`), `def reset_shared_sellable_cache() -> None` (test hook — drops the singleton; mirrors `reset_shared_rate_limiter`, `rate_limiter.py:88-91`).

Steps:

- [ ] **Write the failing tests — TTL semantics, disabled no-op, singleton, deterministic clock.** Append to `tests/test_toss_sellable_cache.py`:
```python
from decimal import Decimal

from app.services import toss_sellable_cache as sc
from app.services.toss_sellable_cache import (
    TossSellableCache,
    get_shared_sellable_cache,
    reset_shared_sellable_cache,
)


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_shared_sellable_cache()
    yield
    reset_shared_sellable_cache()


class TestTossSellableCache:
    def test_hit_within_ttl(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        assert cache.get("BRK.B") is None            # cold miss
        cache.put("BRK.B", Decimal("1.25"))
        clock.advance(44.9)
        assert cache.get("BRK.B") == Decimal("1.25")  # still fresh

    def test_miss_after_ttl_expiry(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        cache.put("BRK.B", Decimal("1.25"))
        clock.advance(45.0)                            # expiry boundary is exclusive
        assert cache.get("BRK.B") is None

    def test_per_symbol_isolation(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now)
        cache.put("AAA", Decimal("3"))
        assert cache.get("BBB") is None
        assert cache.get("AAA") == Decimal("3")

    def test_disabled_is_complete_no_op(self):
        clock = _Clock()
        cache = TossSellableCache(ttl_seconds=45, now=clock.now, enabled=False)
        cache.put("BRK.B", Decimal("1.25"))
        assert cache.get("BRK.B") is None              # never stores => always miss


class TestSingleton:
    def test_shared_instance(self):
        assert get_shared_sellable_cache() is get_shared_sellable_cache()

    def test_reset_drops_instance(self):
        first = get_shared_sellable_cache()
        reset_shared_sellable_cache()
        assert get_shared_sellable_cache() is not first
```

- [ ] **Run it — fails.** `uv run pytest tests/test_toss_sellable_cache.py -v`
  Expected: `ModuleNotFoundError: app.services.toss_sellable_cache`.

- [ ] **Minimal impl — create the module.** Create `app/services/toss_sellable_cache.py`:
```python
"""ROB-701 — process-global short-TTL cache for the Toss per-symbol
sellable-quantity fanout on /invest home & account-panel.

The Toss ``GET /api/v1/sellable-quantity`` endpoint is in the ORDER_INFO
rate-limit group (6 TPS / 3 TPS peak), so fanning it out per holding serializes
to ~N/6 s. This cache collapses repeated /invest loads to 0 calls within the TTL;
only the invest_home reader opts in (the MCP / sell-classification path stays
uncached and fresh). enabled=False => always miss => today's fanout-every-load.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal

from app.core.config import settings


class TossSellableCache:
    def __init__(
        self,
        *,
        ttl_seconds: float,
        now: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._now = now
        self._enabled = enabled
        # symbol -> (expires_at_monotonic, value)
        self._entries: dict[str, tuple[float, Decimal]] = {}

    def get(self, symbol: str) -> Decimal | None:
        if not self._enabled:
            return None
        entry = self._entries.get(symbol)
        if entry is None:
            return None
        expires_at, value = entry
        if self._now() >= expires_at:
            # Expired — evict so the map does not grow unbounded on churn.
            self._entries.pop(symbol, None)
            return None
        return value

    def put(self, symbol: str, value: Decimal) -> None:
        if not self._enabled:
            return
        self._entries[symbol] = (self._now() + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()


_shared_sellable_cache: TossSellableCache | None = None


def get_shared_sellable_cache() -> TossSellableCache:
    """Process-global cache shared by every /invest reader in the process, so a
    warm entry from one surface (home) serves the next (account-panel)."""
    global _shared_sellable_cache
    if _shared_sellable_cache is None:
        _shared_sellable_cache = TossSellableCache(
            ttl_seconds=float(
                getattr(settings, "toss_sellable_cache_ttl_seconds", 45.0)
            ),
            enabled=bool(getattr(settings, "toss_sellable_cache_enabled", True)),
        )
    return _shared_sellable_cache


def reset_shared_sellable_cache() -> None:
    """Test hook: drop the process-global cache so suites start clean."""
    global _shared_sellable_cache
    _shared_sellable_cache = None
```

- [ ] **Run it — passes.** `uv run pytest tests/test_toss_sellable_cache.py -v` → all pass.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "feat(ROB-701): TossSellableCache short-TTL per-symbol cache + module singleton"`

---

## Task 3 — Cache-aware fanout in `fetch_toss_portfolio_snapshot` (migration-0)

**Files:**
- Modify `app/services/toss_portfolio_service.py` — imports (top, near `:10`); `fetch_toss_portfolio_snapshot` signature (`:131-135`); the `need_sellable` branch **including the shared `errors` accumulator** (`:147-177` — `errors: list[dict[str, Any]] = []` at `:147` through the ROB-685 `else` skip at `:177`). The position-build loop (`:179-213`), cash snapshot (`:215-216`), return (`:218-223`), and `finally` (`:224-226`) stay **unchanged**.
- Test (modify) `tests/test_toss_portfolio_service.py` — add cache cases beside the existing `_FakeTossClient` (records `sellable_calls`, `:45-64`).

**Interfaces:**
- Produces `async def fetch_toss_portfolio_snapshot(*, need_sellable: bool = True, sellable_cache: TossSellableCache | None = None, client: TossPortfolioClient | None = None) -> TossPortfolioSnapshot`.
  - `need_sellable=True, sellable_cache=None` → **today's behavior verbatim** (full fanout, span, zip).
  - `need_sellable=True, sellable_cache=cache` → fetch `sellable_quantity` **only** for cache-miss symbols; `cache.put` on success (never on exception); re-wrap cache hits as `TossSellableQuantity(sellable_quantity=<cached>)` so the position-build loop is unchanged; the `invest.home.toss_api.sellable_quantity` span is still opened (now with a `cache_miss_count`).
  - `need_sellable=False` → **today's ROB-685 skip verbatim** (`paired = [(item, None) ...]`), `sellable_cache` ignored.
- Consumes: `TossPortfolioClient.sellable_quantity(*, symbol)` (misses only), `TossSellableCache.get/put`, `TossSellableQuantity` (`app/services/brokers/toss/dto.py:225-227`).

Steps:

- [ ] **Write the failing tests — cache-hit=0 calls, TTL refetch, accuracy from cache, error-not-cached; defaults locked.** Append to `tests/test_toss_portfolio_service.py` (top imports: add `from app.services.toss_sellable_cache import TossSellableCache`):
```python
class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def now(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.mark.asyncio
async def test_snapshot_cache_hit_issues_zero_sellable_calls() -> None:
    clock = _Clock()
    cache = TossSellableCache(ttl_seconds=45, now=clock.now)
    client = _FakeTossClient()

    # Cold load: miss on every symbol => one fanout, cache populated.
    snap1 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B"]
    assert snap1.positions[0].sellable_quantity == Decimal("1.25")

    # Warm load within TTL: ZERO new sellable calls, value served from cache.
    snap2 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B"]  # unchanged => cache hit
    assert snap2.positions[0].sellable_quantity == Decimal("1.25")  # accuracy preserved


@pytest.mark.asyncio
async def test_snapshot_cache_refetches_after_ttl_expiry() -> None:
    clock = _Clock()
    cache = TossSellableCache(ttl_seconds=45, now=clock.now)
    client = _FakeTossClient()

    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    clock.advance(45.0)  # TTL boundary is exclusive => expired
    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B", "BRK.B"]  # refetched after expiry


@pytest.mark.asyncio
async def test_snapshot_cache_does_not_store_failed_fetch() -> None:
    class ErrClient(_FakeTossClient):
        async def sellable_quantity(self, *, symbol: str):
            self.sellable_calls.append(symbol)
            raise RuntimeError(f"boom {symbol}")

    clock = _Clock()
    cache = TossSellableCache(ttl_seconds=45, now=clock.now)
    client = ErrClient()

    snap1 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert snap1.positions[0].sellable_quantity is None
    assert snap1.errors[0]["stage"] == "sellable_quantity"

    # Error was NOT cached => next load within TTL retries the fetch.
    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B", "BRK.B"]


@pytest.mark.asyncio
async def test_snapshot_no_cache_default_still_fans_out() -> None:
    client = _FakeTossClient()
    # sellable_cache defaults to None => today's fanout path, unchanged.
    await fetch_toss_portfolio_snapshot(client=client, need_sellable=True)
    await fetch_toss_portfolio_snapshot(client=client, need_sellable=True)
    assert client.sellable_calls == ["BRK.B", "BRK.B"]


@pytest.mark.asyncio
async def test_snapshot_need_sellable_false_ignores_cache() -> None:
    clock = _Clock()
    cache = TossSellableCache(ttl_seconds=45, now=clock.now)
    client = _FakeTossClient()
    snap = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=False, sellable_cache=cache
    )
    # ROB-685 skip path is untouched: no fanout, no cache read/write.
    assert client.sellable_calls == []
    assert snap.positions[0].sellable_quantity is None
```

- [ ] **Run it — fails.** `uv run pytest tests/test_toss_portfolio_service.py -v -k "cache"`
  Expected: the **four** tests that pass `sellable_cache=` (`..._cache_hit_issues_zero_sellable_calls`, `..._cache_refetches_after_ttl_expiry`, `..._cache_does_not_store_failed_fetch`, `..._need_sellable_false_ignores_cache`) FAIL — `fetch_toss_portfolio_snapshot` has no `sellable_cache` kwarg (`TypeError: unexpected keyword argument`). `test_snapshot_no_cache_default_still_fans_out` does **not** pass `sellable_cache`, so it PASSES today (it is a regression-lock for the unchanged default fanout and keeps passing after impl). After impl all five pass and the last four lock the new/unchanged paths.

- [ ] **Minimal impl — signature + imports.** In `app/services/toss_portfolio_service.py`, add near the existing imports (`:10`):
```python
from app.services.brokers.toss.dto import TossSellableQuantity
from app.services.toss_sellable_cache import TossSellableCache
```
Change the signature at `:131-135`:
```python
async def fetch_toss_portfolio_snapshot(
    *,
    need_sellable: bool = True,
    sellable_cache: TossSellableCache | None = None,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
```

- [ ] **Minimal impl — cache-aware branch.** Replace `:147-177` — the shared `errors: list[dict[str, Any]] = []` accumulator at `:147` **through** the ROB-685 `else` skip at `:177` — with the three-way branch below. The block's leading `errors: list[dict[str, Any]] = []` line **is that same single declaration** (do NOT leave the original `:147` line in place, or you will declare `errors` twice). Keep the existing `need_sellable`-no-cache branch and the `else` (ROB-685 skip) **byte-for-byte**; insert the new cache branch first:
```python
        errors: list[dict[str, Any]] = []

        if need_sellable and sellable_cache is not None:
            # ROB-701: only cache-MISS symbols hit the ORDER_INFO (6 TPS)
            # /sellable-quantity endpoint; hits reuse the cached value. Re-wrap
            # hits as TossSellableQuantity so the position-build loop below is
            # unchanged.
            hits: list[Decimal | None] = [
                sellable_cache.get(item.symbol) for item in holdings.items
            ]
            miss_indices = [i for i, hit in enumerate(hits) if hit is None]
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                span.set_data("cache_miss_count", len(miss_indices))
                fetched = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(
                            symbol=holdings.items[i].symbol
                        )
                        for i in miss_indices
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(1 for result in fetched if isinstance(result, BaseException)),
                )
            fetched_by_index: dict[int, Any] = dict(
                zip(miss_indices, fetched, strict=True)
            )
            for index, result in fetched_by_index.items():
                if not isinstance(result, BaseException):
                    # Cache ONLY successful fetches — a transient error must not
                    # poison the cache (next load retries).
                    sellable_cache.put(
                        holdings.items[index].symbol, result.sellable_quantity
                    )
            paired: list[tuple[Any, Any]] = []
            for index, item in enumerate(holdings.items):
                if index in fetched_by_index:
                    paired.append((item, fetched_by_index[index]))
                else:
                    paired.append(
                        (item, TossSellableQuantity(sellable_quantity=hits[index]))
                    )
        elif need_sellable:
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                sellable_results = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=item.symbol)
                        for item in holdings.items
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(
                        1
                        for result in sellable_results
                        if isinstance(result, BaseException)
                    ),
                )
            paired = list(zip(holdings.items, sellable_results, strict=True))
        else:
            # ROB-685: caller does not consume sellable_quantity — skip the
            # per-holding GET /sellable-quantity (ORDER_INFO, 6 TPS) fanout that
            # otherwise serializes to ~6/sec and dominates wall time.
            paired = [(item, None) for item in holdings.items]
```
(The `positions` build loop, cash snapshot, return, and `finally` are left exactly as-is at `:179-226`. Note `paired` is first annotated in the cache branch; the `elif`/`else` reuse the same name — matching how the current code names it at `:170`.)

- [ ] **Run it — passes.** `uv run pytest tests/test_toss_portfolio_service.py -v` → all pass (5 new cache cases + the 6 pre-existing cases incl. `..._keeps_position_when_sellable_fails` and `..._skips_sellable_when_not_needed`, both untouched).

- [ ] **Regression — phase-span + reader suites unaffected by the no-cache default.** `uv run pytest tests/test_invest_home_readers.py -v -k "toss"`
  Expected: `test_toss_portfolio_snapshot_emits_phase_spans` (`:1712`) still asserts `invest.home.toss_api.sellable_quantity in started` — it calls the service with `client=...` and **no** cache, hitting the unchanged `elif need_sellable` branch. The 3 `test_toss_api_home_reader_*` fakes still pass (Task 4 not yet applied; the reader still calls with no `sellable_cache`).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-701): fetch_toss_portfolio_snapshot cache-aware sellable fanout (miss-only) when sellable_cache passed"`

---

## Task 4 — invest_home reader passes the shared cache when mutations armed (migration-0)

**Files:**
- Modify `app/services/invest_home_readers.py` — import `get_shared_sellable_cache` (near `:39`, the existing `fetch_toss_portfolio_snapshot` import); `TossApiHomeReader.fetch` snapshot call at `:554-556`.
- Test (modify) `tests/test_invest_home_readers.py` — add an assertion test AND update the 4 existing Toss-reader fakes (`fake_fetch_toss_snapshot` at `:1523`, `:1587`, `:1635`, `:1691`) to accept the new `sellable_cache` kwarg.

**Interfaces:**
- Consumes: `mutations_enabled` (already computed at `invest_home_readers.py:547-549`) and `get_shared_sellable_cache()`.
- Produces: the sole `fetch_toss_portfolio_snapshot(...)` call at `:554-556` becomes:
```python
                snapshot = await fetch_toss_portfolio_snapshot(
                    need_sellable=mutations_enabled,
                    sellable_cache=(
                        get_shared_sellable_cache() if mutations_enabled else None
                    ),
                )
```
  When mutations off (default): `need_sellable=False`, `sellable_cache=None` → ROB-685 skip, no cache. When mutations on (prod): `need_sellable=True`, `sellable_cache=<shared>` → cache-aware fanout; the reader still collapses toss sellable via `_toss_sellable_quantity(position, mutations_enabled)` (`:516-524`) so display output is unchanged aside from ≤TTL staleness.

Steps:

- [ ] **Write the failing test — reader passes a cache iff mutations armed.** Add to `tests/test_invest_home_readers.py`:
```python
@pytest.mark.asyncio
@pytest.mark.unit
@pytest.mark.parametrize("mutations", [False, True])
async def test_toss_api_home_reader_passes_sellable_cache_when_mutations_on(
    monkeypatch, mutations
):
    from decimal import Decimal

    from app.core.config import settings as _cfg
    from app.services import invest_home_readers as readers
    from app.services.toss_portfolio_service import TossPortfolioSnapshot
    from app.services.toss_sellable_cache import TossSellableCache

    captured: dict[str, object] = {}

    async def fake_fetch_toss_snapshot(*, need_sellable=True, sellable_cache=None):
        captured["need_sellable"] = need_sellable
        captured["sellable_cache"] = sellable_cache
        return TossPortfolioSnapshot(
            positions=[], cash_krw=Decimal("1"), cash_usd=Decimal("1")
        )

    monkeypatch.setattr(
        readers, "fetch_toss_portfolio_snapshot", fake_fetch_toss_snapshot
    )
    monkeypatch.setattr(
        _cfg, "toss_live_order_mutations_enabled", mutations, raising=False
    )

    await readers.TossApiHomeReader().fetch(user_id=1)

    if mutations:
        # mutations armed => cache is threaded so repeated loads reuse it.
        assert isinstance(captured["sellable_cache"], TossSellableCache)
        assert captured["need_sellable"] is True
    else:
        # mutations off => ROB-685 skip, no cache needed.
        assert captured["sellable_cache"] is None
        assert captured["need_sellable"] is False
```

- [ ] **Run it — fails.** `uv run pytest tests/test_invest_home_readers.py -v -k "passes_sellable_cache"`
  Expected: FAILS — today `TossApiHomeReader.fetch` calls `fetch_toss_portfolio_snapshot(need_sellable=mutations_enabled)` with no `sellable_cache`, so the fake never receives it and (once the fake accepts the kwarg) `captured["sellable_cache"]` is always `None`; the `mutations=True` case fails the `isinstance(..., TossSellableCache)` assert.

- [ ] **Minimal impl — import + thread the cache.** In `app/services/invest_home_readers.py`, add next to the `:39` import:
```python
from app.services.toss_sellable_cache import get_shared_sellable_cache
```
Change the fetch at `:554-556`:
```python
                snapshot = await fetch_toss_portfolio_snapshot(
                    need_sellable=mutations_enabled,
                    sellable_cache=(
                        get_shared_sellable_cache() if mutations_enabled else None
                    ),
                )
```
(No other reader logic changes — `_toss_sellable_quantity` / `_toss_pending_sell_quantity` already gate on `mutations_enabled` at `:516-533`.)

- [ ] **Update the 4 existing Toss-reader fakes to accept the kwarg.** In `tests/test_invest_home_readers.py`, change each `async def fake_fetch_toss_snapshot(*, need_sellable: bool = True):` (at `:1523`, `:1587`, `:1635`, and the gating test's at `:1691`) to `async def fake_fetch_toss_snapshot(*, need_sellable: bool = True, sellable_cache=None):`. Behavior is unchanged; this keeps them call-compatible with the new keyword. (The phase-span test at `:1712` calls the real service with `client=...` and no cache, so it is unaffected.)

- [ ] **Run it — passes.** `uv run pytest tests/test_invest_home_readers.py -v -k "toss"` → all pass (new cache-threading test both params + the updated 4 fakes + gating test + phase-span test).

- [ ] **Regression — full reader suite + service + cache.** `uv run pytest tests/test_invest_home_readers.py tests/test_toss_portfolio_service.py tests/test_toss_sellable_cache.py -q` → no failures.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-701): invest_home toss reader threads shared sellable cache when mutations armed"`

---

## Done criteria

- Repeated `/invest` home / account-panel loads (mutations armed) issue **0** `GET /sellable-quantity` calls within the cache TTL — the first load pays one bounded fanout, later loads hit the cache. Proven by `test_snapshot_cache_hit_issues_zero_sellable_calls`.
- TTL expiry refetches (`test_snapshot_cache_refetches_after_ttl_expiry`); a failed fetch is never cached (`test_snapshot_cache_does_not_store_failed_fetch`).
- Accuracy preserved: `TossPortfolioPosition.sellable_quantity` (→ `Holding.sellableQuantity`) equals the source value, with ≤TTL staleness. ROB-549 display + `_toss_sellable_quantity` gating unchanged.
- The ROB-685 `need_sellable=False` fast path is byte-for-byte unchanged (`test_snapshot_need_sellable_false_ignores_cache`, plus the pre-existing skip test).
- The default no-cache path (`sellable_cache=None`) — every MCP / sell-classification consumer — fans out exactly as today (`test_snapshot_no_cache_default_still_fans_out`); `sellable_quantity()` semantics and the `ORDER_INFO` 6-TPS group are untouched.
- `make lint` clean; no alembic revision added (migration-0).
