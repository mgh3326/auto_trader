# get_sector_peers — Bound the Naver Fanout (Semaphore) + Short-TTL Cache + Trim Over-fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax.

**Goal:** Make the KR (`market="kr"`) path of `get_sector_peers` stop amplifying one MCP tool call into a ~29-request concurrent burst against `m.stock.naver.com` that trips Naver's server-side throttling and stalls individual calls out to the 10s httpx timeout (Sentry 7d: p95 = 11.5s, 24 calls > 9s, sum 1.40M ms). Four surgical, independently-testable fixes in `app/services/naver_finance/valuation.py::fetch_sector_peers`: (1) **[load-bearing]** bound the peer fanout with an `asyncio.Semaphore` so the same calls run at ~676ms unthrottled instead of colliding into timeout stragglers; (2) add a peer-only, per-request short timeout for fast-fail on a stuck peer without breaking the currently-succeeding ~10s target fetch; (3) trim the over-fetch so the `limit + 5` padding is only paid when the integration endpoint returned fewer than `limit` peers; (4) add a short-TTL, fail-open Redis cache (cache-aside, keyed by stock code for the `/basic`+`/integration` bundle and by industry code for the sector page) for the recurring-symbol hits observed in Sentry. Read-only advisory path, migration-0.

**Architecture:** Today `fetch_sector_peers(code, limit=5)` (`app/services/naver_finance/valuation.py:356`) opens one shared `httpx.AsyncClient(timeout=10)` (`:380`), does a bare `await _fetch_integration(code, client)` for the target (`:384`, NOT wrapped in `_safe_fetch`), fetches the sector page once (`_fetch_sector_soup`, `:396-397`), pads the peer-code list to `peer_codes[: limit + 5]` unconditionally (`:409`), and then fires **all** peer fetches through an unbounded `asyncio.gather(*[_safe_fetch(pc) ...])` (`:417`) — each `_safe_fetch` (`:411`) calls `_fetch_integration` which itself is 2 requests (`/basic` + `/integration`, `asyncio.gather` at `:316`). With `limit=5` that is `(limit+5) × 2 = 20` peer requests + 2 target + 1 sector ≈ 23–29 simultaneous connections; Naver throttles the burst and tail calls stall to the 10s client timeout. There is no cache, so recurring symbols (Sentry: `006400`/`450080`/`247540`/`043260` each 2×/7d) re-pay the full burst every call.

After the fix, `fetch_sector_peers` (a) acquires a per-call `asyncio.Semaphore(settings.naver_peer_fetch_concurrency)` inside `_safe_fetch` so at most N peer fetches (~5) are in flight — the burst that trips throttling never forms; (b) passes a short `request_timeout` (`settings.naver_peer_fetch_timeout_seconds`, ~5s) into `_fetch_integration` for peers only, while the target keeps the client-level 10s (its bare await at `:384` is unchanged and still succeeds); (c) only appends sector-scraped extras and pads to `limit + 5` when the integration endpoint returned `< limit` peers — otherwise it caps to `limit`, cutting peer requests roughly in half on the common path; and (d) routes every `_fetch_integration` call through a fail-open cache-aside wrapper (`app/services/naver_finance/peer_cache.py`) so a cache hit within the short TTL skips the network entirely. The sector page fetch (`:396-397`) stays exactly once per call and remains the sole source of the output `sector` **name**.

**Tech Stack:** Python 3.13, uv, pytest (markers `unit`/`asyncio`), httpx `AsyncClient` (per-request `timeout=` override), `asyncio.Semaphore`, `redis.asyncio` (fail-open cache-aside mirroring `app/core/analyze_cache.py`), pydantic-settings (`app/core/config.Settings`), BeautifulSoup4/lxml (Naver scrape), FastMCP tool surface (`get_sector_peers`).

## Global Constraints

Copied verbatim; every task implicitly includes these.

- **KEEP the sector-name scrape:** sise_group_detail.naver (sector soup) is the ONLY source of the output "sector" NAME (integration returns only industry_code). Do NOT "skip the sector scrape when integration peers >= limit" — that nulls the sector field on the common path. It is dual-purpose (extra peers AND sector name).
- **Fix (4): the timeout must be a PEER-ONLY / per-request timeout.** The target fetch (valuation.py:~384) is a bare await NOT wrapped in _safe_fetch and shares the client; lowering the global client timeout would make a currently-succeeding ~10s target fast-fail into a whole-tool error.
- **Read-only advisory (no broker/order/watch mutation).** Caching only makes current_price/change_pct up to TTL stale (acceptable for a sector-comparison tool).
- **migration-0.** No new DB column, table, or alembic revision. The only additive persistence is Redis (ephemeral, TTL'd). New config fields on `app/core/config.Settings` are runtime-tunable knobs, not schema.
- **Fail-open everywhere.** The Semaphore, the short peer timeout, the trim, and the cache must each degrade to *at worst* today's behavior: a peer fetch failure already returns `None` from `_safe_fetch` and is dropped from the peer list; a cache/Redis outage must fall through to the live fetch and never raise; a peer-timeout must not surface as a whole-tool error.
- Run tests: `uv run pytest <path> -v`. Lint: `make lint`. Do NOT commit unless the executing skill says so; each task lists its own commit message.

---

## Approach / decision note

- **Cache = Redis short-TTL cache-aside, NOT a persisted-snapshot precompute.** Lower risk and satisfies migration-0: it reuses the exact fail-open pattern already shipped in `app/core/analyze_cache.py` (lazy module-level client, settings gate, `_normalize_cache_envelope`, best-effort GET/SET that never raise). No new table, no scheduler/TaskIQ, no cutover. A cache/Redis outage transparently degrades to today's direct fetch. Precompute would need a table + a refresh job + a migration — rejected.
- **Knobs live on `Settings` (runtime resolution), not module constants.** `naver_peer_fetch_concurrency` (default `5`), `naver_peer_fetch_timeout_seconds` (default `5.0`), `naver_peer_cache_enabled` (default `True`), `naver_peer_cache_ttl_seconds` (default `600` = 10 min intraday). Operators tune without a code change; tests monkeypatch `settings`. `naver_peer_cache_enabled` is force-disabled in `tests/conftest.py` (same hermetic guard as `ANALYZE_FETCH_CACHE_ENABLED`) so no test can touch a real Redis.
- **The Semaphore is created per-call inside `fetch_sector_peers`, not as a module-level singleton.** A module-level `asyncio.Semaphore` binds to the event loop that constructed it; pytest-asyncio (and the MCP server's task lifecycle) use fresh loops, so a singleton would raise "bound to a different event loop". A fresh `asyncio.Semaphore(n)` per call is the standard safe pattern and is trivially test-isolated.
- **Concurrency default 5** (finding suggested 4–6): comfortably below Naver's throttle threshold while keeping the common `limit=5` (~10–12 peer requests after trim) to ~2–3 sequential waves.

---

## File Structure

| File | Create/Modify | Responsibility (which Task) |
|------|---------------|------------------------------|
| `app/core/config.py` | Modify | Task 1 — `naver_peer_fetch_concurrency`. Task 2 — `naver_peer_fetch_timeout_seconds`. Task 4 — `naver_peer_cache_enabled` + `naver_peer_cache_ttl_seconds`. |
| `app/services/naver_finance/valuation.py` | Modify | Task 1 — per-call `Semaphore` around `_safe_fetch`. Task 2 — `request_timeout` param on `_fetch_integration`; peers pass short timeout, target keeps client default. Task 3 — trim `limit + 5` padding to only fire when integration peers `< limit`. Task 4 — route target + peer fetches through `_fetch_integration_cached`; cache the sector-page derivation. |
| `app/services/naver_finance/peer_cache.py` | Create | Task 4 — fail-open Redis cache-aside helpers (`_get_redis_client`, `get_cached_integration`/`set_cached_integration`, `get_cached_sector`/`set_cached_sector`) mirroring `app/core/analyze_cache.py`. |
| `app/services/naver_finance/__init__.py` | Modify | Task 4 — re-export the new `peer_cache` helpers (optional, only if tests import via the package façade). |
| `tests/conftest.py` | Modify | Task 4 — force `NAVER_PEER_CACHE_ENABLED=false` in `_ensure_test_env` (hermetic guard). |
| `tests/test_naver_finance.py` | Modify | Task 1/2/3 — append new test classes mirroring `TestFetchSectorPeers` (`:1505`). |
| `tests/test_naver_peer_cache.py` | Create | Task 4 — cache-aside unit tests (hit/miss/fail-open/TTL/disabled). |

> **NOT touched:** `app/mcp_server/tooling/fundamentals/_sector_peers.py` (the `handle_get_sector_peers` dispatcher, `:32`) and `app/mcp_server/tooling/fundamentals_sources_naver.py::_fetch_sector_peers_naver` (`:134`) keep their signatures and behavior — the KR entry still calls `naver_finance.fetch_sector_peers(symbol, limit=limit)` (`:137`). **All fanout call sites funnel through this single `fetch_sector_peers`, so fixing it covers every caller — no other wiring needs to change.** Note the effective `limit` is often larger than the `5` this plan uses in examples: `handle_get_sector_peers` caps at `20` (`_sector_peers.py:44` `min(max(limit, 1), 20)`), and the analyze/fundamentals flows call `_fetch_sector_peers_naver(symbol, limit=10)` (`app/analysis/stages/fundamentals_stage.py:22`, `app/mcp_server/tooling/analysis_analyze.py:482`) — those `limit=10`/`20` paths are where the largest bursts (and the biggest wins) occur. Every fix here (Semaphore, peer timeout, trim, cache) is limit-agnostic. The US path (`_fetch_sector_peers_us`) and crypto rejection are out of scope. `_parse_peer_comparison`, `_parse_industry_info`, `_parse_sector_name`, `_parse_sector_stock_codes`, and `fetch_valuation`'s own `_fetch_integration` overlay (`:264-273`) are behavior-unchanged. This plan deliberately confines the fix to `fetch_sector_peers` + one new cache module + config knobs to avoid collision with any concurrent fundamentals work.

---

## Task 1 — Bound the peer fanout with an `asyncio.Semaphore` (load-bearing, migration-0)

**Files:**
- Modify `app/core/config.py:315` (add `naver_peer_fetch_concurrency` near the other cache/knob fields around `:299-315`).
- Modify `app/services/naver_finance/valuation.py:411` (`_safe_fetch`) and `:417` (unbounded `asyncio.gather`) inside `fetch_sector_peers`.
- Test (modify) `tests/test_naver_finance.py` — append `TestFetchSectorPeersConcurrency` after `TestFetchSectorPeers` (`:1505`).

**Interfaces:**
- Consumes `settings.naver_peer_fetch_concurrency: int` (new, default `5`).
- Produces: `fetch_sector_peers(code, limit=5)` signature UNCHANGED. Internally, `_safe_fetch(pc)` acquires a per-call `asyncio.Semaphore(max(1, settings.naver_peer_fetch_concurrency))` created once per `fetch_sector_peers` invocation before awaiting `_fetch_integration`.

Steps:

- [ ] **Write failing test — peak in-flight peer fetches never exceeds the configured cap.** Append to `tests/test_naver_finance.py`:
```python
@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersConcurrency:
    async def test_peer_fanout_is_bounded_by_semaphore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 3)

        # Target returns 8 integration peers so, pre-trim, 8 peer fetches queue.
        peers_raw = [{"itemCode": f"00000{i}"} for i in range(1, 9)]

        in_flight = 0
        peak = 0

        async def fake_fetch_integration(
            code: str, _client: Any, *args: Any, **kwargs: Any
        ) -> dict[str, Any]:
            nonlocal in_flight, peak
            if code == "000100":  # target
                return {
                    "symbol": code, "name": "Target", "per": 10, "pbr": 1.1,
                    "market_cap": 1000, "current_price": 50000, "change_pct": 1.0,
                    "industry_code": "123", "peers_raw": peers_raw,
                }
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)  # hold the slot so overlap is observable
            in_flight -= 1
            return {
                "symbol": code, "name": "Peer", "per": 11, "pbr": 1.2,
                "market_cap": 900, "current_price": 40000, "change_pct": 0.5,
                "industry_code": "123", "peers_raw": [],
            }

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=8)

        assert peak <= 3, f"peak in-flight {peak} exceeded semaphore cap 3"
        assert len(result["peers"]) == 8
```
  (Note: `import asyncio` is already at the top of `valuation.py` (`:5`); the test module (`tests/test_naver_finance.py`) does **not** currently import `asyncio` at the top, and the fake above uses `await asyncio.sleep(0.01)` — so add a top-level `import asyncio` to the test module header. `Any` is already imported at the test module top.)

- [ ] **Run it — fails.** `uv run pytest tests/test_naver_finance.py -k Concurrency -v`
  Expected: `peak` reaches 8 (unbounded `asyncio.gather` at `:417` runs all peer fetches concurrently) → `assert peak <= 3` FAILS.

- [ ] **Add the setting.** In `app/core/config.py`, near the other knob fields (after `:315` `analyze_fetch_cache_enabled`), add:
```python
    # ROB-688: bound the get_sector_peers KR peer fanout so the concurrent burst
    # to m.stock.naver.com stops tripping Naver server-side throttling.
    naver_peer_fetch_concurrency: int = 5
```

- [ ] **Minimal impl — wrap `_safe_fetch` in a per-call Semaphore.** In `app/services/naver_finance/valuation.py`, inside `fetch_sector_peers`, replace the `_safe_fetch` definition (`:411-415`) and its use (`:417`):
```python
        # ---- Fetch integration data for each peer concurrently (bounded) ----
        peer_codes = peer_codes[: limit + 5]  # (Task 3 makes this conditional)

        semaphore = asyncio.Semaphore(max(1, settings.naver_peer_fetch_concurrency))

        async def _safe_fetch(pc: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await _fetch_integration(pc, client)
                except Exception:
                    return None

        peer_results = await asyncio.gather(*[_safe_fetch(pc) for pc in peer_codes])
```
  Add `from app.core.config import settings` to the imports at the top of `valuation.py` (verify it is not already imported).

- [ ] **Run it — passes.** `uv run pytest tests/test_naver_finance.py -k Concurrency -v` → passes (`peak <= 3`).

- [ ] **Regression — existing sector-peers test still green.** `uv run pytest tests/test_naver_finance.py -k "SectorPeers or FetchValuation" -v` → all pass (the existing `test_fetches_sector_page_once_for_codes_and_name` at `:1506` still asserts `len(sector_gets) == 1` and `peers[0]["symbol"] == "000002"`; the Semaphore does not change call counts).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-688): bound get_sector_peers KR peer fanout with asyncio.Semaphore (load-bearing)"`

---

## Task 2 — Peer-only per-request short timeout (fast-fail, migration-0)

**Files:**
- Modify `app/core/config.py` (add `naver_peer_fetch_timeout_seconds` beside the Task 1 field).
- Modify `app/services/naver_finance/valuation.py:307-319` (`_fetch_integration` — add `request_timeout` param, apply to both `client.get`s) and the peer call site inside `fetch_sector_peers` (`_safe_fetch`, from Task 1). The target call at `:384` is LEFT with no `request_timeout` (keeps the client-level 10s).
- Test (modify) `tests/test_naver_finance.py` — append `TestFetchSectorPeersPeerTimeout`.

**Interfaces:**
- Consumes `settings.naver_peer_fetch_timeout_seconds: float` (new, default `5.0`).
- Produces `_fetch_integration(code, client, request_timeout: float | None = None)` — when `request_timeout` is not None it is passed as `client.get(url, timeout=request_timeout)` to each of the `/basic` and `/integration` requests; when None the client-level default (10s) applies. **Backward-compatible:** the existing 2-arg call `_fetch_integration(code, client)` (used by `fetch_valuation` at `:266` and by the target fetch at `:384`) is unchanged.

Steps:

- [ ] **Write failing test — peers get the short per-request timeout, target keeps the client default.** Append to `tests/test_naver_finance.py`:
```python
@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersPeerTimeout:
    async def test_peers_use_short_request_timeout_target_uses_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_timeout_seconds", 5.0)
        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 5)

        # Record the request_timeout each _fetch_integration call receives.
        seen: dict[str, float | None] = {}

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            seen[code] = request_timeout
            base = {
                "symbol": code, "name": code, "per": 10, "pbr": 1.0,
                "market_cap": 100, "current_price": 1, "change_pct": 0.0,
                "industry_code": "123", "peers_raw": [],
            }
            if code == "000100":
                base["peers_raw"] = [{"itemCode": "000200"}]
            return base

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        await naver_finance.fetch_sector_peers("000100", limit=1)

        assert seen["000100"] is None, "target must keep the client-level 10s timeout"
        assert seen["000200"] == 5.0, "peer must use the short per-request timeout"
```

- [ ] **Run it — fails.** `uv run pytest tests/test_naver_finance.py -k PeerTimeout -v`
  Expected: `seen["000200"]` is `None` (peers currently call `_fetch_integration(pc, client)` with no `request_timeout`) → `assert seen["000200"] == 5.0` FAILS. (The `fake_fetch_integration` already declares the param, so the earlier tasks' fakes stay compatible.)

- [ ] **Add the setting.** In `app/core/config.py`, beside the Task 1 field:
```python
    naver_peer_fetch_timeout_seconds: float = 5.0
```

- [ ] **Minimal impl part A — `_fetch_integration` accepts `request_timeout`.** In `app/services/naver_finance/valuation.py`, change the signature (`:307-310`) and the two-request gather (`:316-319`):
```python
async def _fetch_integration(
    code: str,
    client: httpx.AsyncClient,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    ...
    get_kwargs: dict[str, Any] = {}
    if request_timeout is not None:
        get_kwargs["timeout"] = request_timeout
    r_basic, r_integ = await asyncio.gather(
        client.get(f"{NAVER_MOBILE_API}/{code}/basic", **get_kwargs),
        client.get(f"{NAVER_MOBILE_API}/{code}/integration", **get_kwargs),
    )
```
  (httpx `AsyncClient.get` accepts a per-request `timeout=` that overrides the client-level timeout for that request only.)

- [ ] **Minimal impl part B — peers pass the short timeout; target does not.** In `fetch_sector_peers`, update `_safe_fetch` (from Task 1) so the peer fetch passes the configured peer timeout:
```python
        async def _safe_fetch(pc: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await _fetch_integration(
                        pc,
                        client,
                        request_timeout=settings.naver_peer_fetch_timeout_seconds,
                    )
                except Exception:
                    return None
```
  Leave the target fetch at `:384` as `target = await _fetch_integration(code, client)` — no `request_timeout`, so it keeps the client-level 10s (constraint: the bare-await target must not fast-fail).

- [ ] **Update the EXISTING sector-peers test fake to accept `request_timeout` (REQUIRED — this is a real regression, not a no-op).** The existing `TestFetchSectorPeers::test_fetches_sector_page_once_for_codes_and_name` (`tests/test_naver_finance.py:1542-1545`) monkeypatches `_fetch_integration` with a **2-arg** fake `async def fake_fetch_integration(code, _client)`. After part B the peer path calls `_fetch_integration(pc, client, request_timeout=...)`, so that fake raises `TypeError: unexpected keyword argument 'request_timeout'`; `_safe_fetch` swallows it → peer `000002` is dropped → `result["peers"][0]` raises `IndexError` and the test FAILS. (After Task 4 the *target* also routes through `_fetch_integration_cached(..., request_timeout=None)`, so the same 2-arg fake breaks the un-wrapped target call → whole-tool error.) Widen its signature — behavior otherwise unchanged:
```python
        async def fake_fetch_integration(
            code: str,
            _client: Any,
            request_timeout: float | None = None,
        ) -> dict[str, Any]:
```
  (Note: the `TestFetchValuation` fakes at `:1307/:1324/:1353` are also 2-arg but are SAFE — `fetch_valuation` is unchanged and still calls `_fetch_integration(code, client)` with two *positional* args, never the `request_timeout` keyword — so leave them as-is.)

- [ ] **Run it — passes.** `uv run pytest tests/test_naver_finance.py -k "PeerTimeout or SectorPeers" -v` → passes (the widened existing fake keeps `test_fetches_sector_page_once_for_codes_and_name` green).

- [ ] **Regression — `_fetch_integration` callers unchanged.** `uv run pytest tests/test_naver_finance.py -k "FetchValuation or SectorPeers or ParseTotalInfos" -v` → all pass (the overlay call in `fetch_valuation` at `:266` still uses the 2-arg positional form; default `request_timeout=None` preserves 10s behavior; the `SectorPeers` test passes only because the previous step widened its fake).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-688): peer-only per-request timeout for get_sector_peers fast-fail (target keeps 10s)"`

---

## Task 3 — Trim the over-fetch: only pad `limit + 5` when integration peers `< limit` (migration-0)

**Files:**
- Modify `app/services/naver_finance/valuation.py:388-409` inside `fetch_sector_peers` — capture the integration peer count, gate the sector-scraped extras + the `limit + 5` padding on `count < limit`, and cap to `limit` otherwise. The sector-page fetch (`:396-397`) and the sector-name parse (`:420-421`) stay UNCONDITIONAL when `industry_code` is present.
- Test (modify) `tests/test_naver_finance.py` — append `TestFetchSectorPeersTrim`.

**Interfaces:**
- Produces: `fetch_sector_peers` UNCHANGED signature. Internal behavior: when the `/integration` endpoint already returned `>= limit` peer codes, exactly `limit` peer fetches are issued (no `+5` extras, no scrape-derived padding), while the sector page is still fetched once and the output `sector` name is still populated.

Steps:

- [ ] **Write failing test — enough integration peers ⇒ only `limit` peer fetches, sector name still present.** Append to `tests/test_naver_finance.py`:
```python
@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersTrim:
    async def test_no_overfetch_when_integration_has_enough_peers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 10)

        # Integration returns 8 peers; limit=5 -> should fetch exactly 5, not 10.
        peers_raw = [{"itemCode": f"90000{i}"} for i in range(1, 9)]
        fetched_peer_codes: list[str] = []

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            if code != "000100":
                fetched_peer_codes.append(code)
            base = {
                "symbol": code, "name": code, "per": 10, "pbr": 1.0,
                "market_cap": 100, "current_price": 1, "change_pct": 0.0,
                "industry_code": "123", "peers_raw": [],
            }
            if code == "000100":
                base["peers_raw"] = peers_raw
            return base

        sector_gets: list[Any] = []

        class FakeResponse:
            content = (
                "<html><head><title>반도체 : Npay 증권</title></head>"
                "<body><table class='type_5'>"
                "<tr><td><a href='/item/main.naver?code=777777'>P</a></td></tr>"
                "</table></body></html>"
            ).encode("euc-kr")

        class FakeClient:
            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                sector_gets.append((url, params))
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=5)

        assert len(fetched_peer_codes) == 5, (
            f"expected 5 peer fetches, got {len(fetched_peer_codes)}"
        )
        # sector name still resolved from the scrape (dual-purpose page)
        assert result["sector"] == "반도체"
        # sector page still fetched exactly once (never skipped)
        assert len(sector_gets) == 1
        # scrape-derived extra (777777) must NOT appear — integration peers sufficed
        assert "777777" not in fetched_peer_codes
```

- [ ] **Run it — fails.** `uv run pytest tests/test_naver_finance.py -k Trim -v`
  Expected: current code sets `peer_codes = peer_codes[: limit + 5]` unconditionally (`:409`) → 8 integration peers padded to `min(8, 10) = 8` fetches → `assert len(...) == 5` FAILS.

- [ ] **Minimal impl — gate the extras + padding on `integration_count < limit`.** In `app/services/naver_finance/valuation.py`, restructure `:388-409`:
```python
        # ---- Collect peer codes from integration response ----
        peer_codes: list[str] = []
        for p in target["peers_raw"]:
            pc = p.get("itemCode", "")
            if pc and pc != code:
                peer_codes.append(pc)

        integration_peer_count = len(peer_codes)

        industry_code = target.get("industry_code")
        sector_soup = None
        if industry_code:
            # Sector page is dual-purpose: sector NAME (always) + extra peers.
            # Fetch it once regardless of whether we need extras (constraint).
            sector_soup = await _fetch_sector_soup(str(industry_code), client)

        if integration_peer_count < limit:
            # Not enough peers from integration — pad with sector-scraped codes,
            # then fetch a few extras in case some fail.
            if sector_soup is not None:
                extra_codes = _parse_sector_stock_codes(sector_soup)
                seen = {code, *peer_codes}
                for ec in extra_codes:
                    if ec not in seen:
                        peer_codes.append(ec)
                        seen.add(ec)
            peer_codes = peer_codes[: limit + 5]
        else:
            # Integration already has enough peers — no over-fetch padding.
            peer_codes = peer_codes[:limit]
```
  Note: the old `:400-406` block that appended `extra_codes` now lives inside the `if integration_peer_count < limit:` branch, and the unconditional `peer_codes = peer_codes[: limit + 5]` at `:409` is removed (the Task 1 line `peer_codes = peer_codes[: limit + 5]  # (Task 3 makes this conditional)` is superseded by this block). Leave the sector-name parse (`:420-421`, `if sector_soup is not None: sector_name = _parse_sector_name(sector_soup)`) exactly as-is so the name is always populated.

- [ ] **Run it — passes.** `uv run pytest tests/test_naver_finance.py -k Trim -v` → passes.

- [ ] **Regression — fewer-than-limit path still scrapes extras.** `uv run pytest tests/test_naver_finance.py -k "SectorPeers or Concurrency or PeerTimeout" -v` → all pass. In particular `test_fetches_sector_page_once_for_codes_and_name` (`:1506`, target has 0 integration peers, limit=1 ⇒ `0 < 1` branch ⇒ scrape extras ⇒ peer `000002`, sector `반도체`, `len(sector_gets) == 1`) stays green (its fake was widened to accept `request_timeout` in Task 2).

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-688): trim get_sector_peers over-fetch — pad limit+5 only when integration peers < limit"`

---

## Task 4 — Short-TTL fail-open Redis cache for the integration bundle + sector page (migration-0)

**Files:**
- Create `app/services/naver_finance/peer_cache.py` — fail-open cache-aside helpers mirroring `app/core/analyze_cache.py` (`_get_redis_client`, `get_cached_integration`/`set_cached_integration` keyed by code, `get_cached_sector`/`set_cached_sector` keyed by industry code, `close_peer_cache_redis`).
- Modify `app/core/config.py` — `naver_peer_cache_enabled: bool = True`, `naver_peer_cache_ttl_seconds: int = 600`.
- Modify `tests/conftest.py:106` — add `os.environ["NAVER_PEER_CACHE_ENABLED"] = "false"` in `_ensure_test_env`.
- Modify `app/services/naver_finance/valuation.py` — add `_fetch_integration_cached(code, client, redis_client, *, request_timeout=None)`; route the target (`:384`) and peer (`_safe_fetch`) fetches through it; cache the sector-page derivation (`{sector_name, extra_codes}`) around `_fetch_sector_soup`.
- Optionally modify `app/services/naver_finance/__init__.py` to re-export `peer_cache` helpers (only if the cache test imports via the package façade).
- Test (create) `tests/test_naver_peer_cache.py`.

**Interfaces:**
- Consumes `settings.naver_peer_cache_enabled: bool`, `settings.naver_peer_cache_ttl_seconds: int`.
- Produces (in `peer_cache.py`):
  - `async _get_redis_client() -> redis.Redis | None` — returns `None` when `naver_peer_cache_enabled` is False or client init fails (hermetic + fail-open).
  - `async get_cached_integration(redis_client, code) -> dict[str, Any] | None` / `async set_cached_integration(redis_client, code, payload) -> None` — key `naver_peer:integ:{CODE}`, TTL `naver_peer_cache_ttl_seconds`. Never raise.
  - `async get_cached_sector(redis_client, industry_code) -> dict[str, Any] | None` / `async set_cached_sector(redis_client, industry_code, payload) -> None` — key `naver_peer:sector:{INDUSTRY}`, payload `{"sector_name": str|None, "extra_codes": list[str]}`, TTL as above. Never raise.
- Produces (in `valuation.py`) `_fetch_integration_cached(code, client, redis_client, *, request_timeout=None) -> dict[str, Any]` — cache-aside over `_fetch_integration`. On hit returns the cached dict (skips network); on miss fetches live and best-effort caches non-degraded results. `fetch_sector_peers` opens one `redis_client = await peer_cache._get_redis_client()` per call and threads it through target + peers + sector.

Steps:

- [ ] **Write failing test — cache-aside hit skips network; disabled/None fails open.** Create `tests/test_naver_peer_cache.py`:
```python
from __future__ import annotations

from typing import Any

import pytest

from app.services.naver_finance import peer_cache

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex))


async def test_set_then_get_integration_roundtrips_with_ttl(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "naver_peer_cache_ttl_seconds", 600)
    r = _FakeRedis()
    payload = {"symbol": "006400", "per": 12.3, "market_cap": 999, "peers_raw": []}
    await peer_cache.set_cached_integration(r, "006400", payload)
    assert r.set_calls and r.set_calls[0][2] == 600  # TTL applied

    got = await peer_cache.get_cached_integration(r, "006400")
    assert got == payload


async def test_get_with_none_client_returns_none_fail_open():
    assert await peer_cache.get_cached_integration(None, "006400") is None
    # set with None client is a no-op (must not raise)
    await peer_cache.set_cached_integration(None, "006400", {"a": 1})


async def test_get_client_returns_none_when_disabled(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "naver_peer_cache_enabled", False)
    assert await peer_cache._get_redis_client() is None


async def test_malformed_cache_value_returns_none(monkeypatch):
    r = _FakeRedis()
    r.store["naver_peer:integ:006400"] = "{not json"
    assert await peer_cache.get_cached_integration(r, "006400") is None


async def test_sector_payload_roundtrips(monkeypatch):
    r = _FakeRedis()
    payload = {"sector_name": "반도체", "extra_codes": ["000660", "000990"]}
    await peer_cache.set_cached_sector(r, "123", payload)
    assert await peer_cache.get_cached_sector(r, "123") == payload
```

- [ ] **Run it — fails.** `uv run pytest tests/test_naver_peer_cache.py -v`
  Expected: `ModuleNotFoundError: app.services.naver_finance.peer_cache` (module does not exist yet).

- [ ] **Add the settings + conftest guard.** In `app/core/config.py` (beside the Task 1/2 fields):
```python
    # ROB-688: short-TTL fail-open Redis cache for the get_sector_peers KR
    # /basic+/integration bundle and the sector page. Intraday staleness of
    # current_price/change_pct up to the TTL is acceptable for a comparison tool.
    naver_peer_cache_enabled: bool = True
    naver_peer_cache_ttl_seconds: int = 600
```
  In `tests/conftest.py`, inside `_ensure_test_env` right after the `ANALYZE_FETCH_CACHE_ENABLED` line (`:106`):
```python
    # ROB-688: same hermetic guard for the sector-peers cache — never touch a
    # real Redis from tests; cache tests inject a fake client explicitly.
    os.environ["NAVER_PEER_CACHE_ENABLED"] = "false"
```

- [ ] **Minimal impl — create `peer_cache.py`.** New file `app/services/naver_finance/peer_cache.py`, mirroring `app/core/analyze_cache.py`'s fail-open contract (lazy singleton client, settings gate, `json.dumps(default=str)`, GET/SET that never raise):
```python
"""ROB-688 — short-TTL fail-open Redis cache for get_sector_peers KR fetches.

Cache-aside over the Naver mobile /basic+/integration bundle (keyed by stock
code) and the sector detail page derivation (keyed by industry code). Mirrors
the fail-open contract of app.core.analyze_cache: any Redis outage or malformed
payload degrades to a live fetch and never raises. Gated by
settings.naver_peer_cache_enabled (forced off in tests/conftest.py).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis

from app.core.config import settings
from app.services.ohlcv_cache_common import create_redis_client

logger = logging.getLogger(__name__)

_INTEG_PREFIX = "naver_peer:integ:"
_SECTOR_PREFIX = "naver_peer:sector:"
_REDIS_CLIENT: redis.Redis | None = None


async def _get_redis_client() -> redis.Redis | None:
    global _REDIS_CLIENT
    if not settings.naver_peer_cache_enabled:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    try:
        _REDIS_CLIENT = await create_redis_client()
    except Exception as exc:  # noqa: BLE001 — fail open to live fetch
        logger.debug("peer_cache: redis init failed: %s", exc)
        _REDIS_CLIENT = None
    return _REDIS_CLIENT


async def close_peer_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        try:
            await _REDIS_CLIENT.close()
        except Exception:  # noqa: BLE001
            pass
        _REDIS_CLIENT = None


def _parse_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def _get(redis_client: redis.Redis | None, key: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("peer_cache: GET failed for %s: %s", key, exc)
        return None
    return _parse_dict(raw)


async def _set(redis_client: redis.Redis | None, key: str, payload: dict[str, Any]) -> None:
    if redis_client is None:
        return
    try:
        ttl = max(1, int(settings.naver_peer_cache_ttl_seconds))
        serialized = json.dumps(payload, default=str, ensure_ascii=False)
        await redis_client.set(key, serialized, ex=ttl)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("peer_cache: SET failed for %s: %s", key, exc)


async def get_cached_integration(
    redis_client: redis.Redis | None, code: str
) -> dict[str, Any] | None:
    return await _get(redis_client, f"{_INTEG_PREFIX}{code.upper()}")


async def set_cached_integration(
    redis_client: redis.Redis | None, code: str, payload: dict[str, Any]
) -> None:
    await _set(redis_client, f"{_INTEG_PREFIX}{code.upper()}", payload)


async def get_cached_sector(
    redis_client: redis.Redis | None, industry_code: str
) -> dict[str, Any] | None:
    return await _get(redis_client, f"{_SECTOR_PREFIX}{industry_code}")


async def set_cached_sector(
    redis_client: redis.Redis | None, industry_code: str, payload: dict[str, Any]
) -> None:
    await _set(redis_client, f"{_SECTOR_PREFIX}{industry_code}", payload)
```

- [ ] **Run the cache unit tests — pass.** `uv run pytest tests/test_naver_peer_cache.py -v` → all pass (note `_get_redis_client` disabled-path test flips `naver_peer_cache_enabled` True via monkeypatch is unnecessary — conftest already forces it False; the disabled test asserts None).

- [ ] **Write failing test — `fetch_sector_peers` serves the target from the integration cache (no network for it).** Append to `tests/test_naver_finance.py` a `TestFetchSectorPeersCache` class that (a) monkeypatches `settings.naver_peer_cache_enabled = True`, (b) patches `naver_finance.peer_cache._get_redis_client` to return a fake redis pre-seeded with `naver_peer:integ:000100` = the target payload, (c) patches `_fetch_integration` to raise if called for `000100` (proving the cache short-circuits the target), returning normal data for peers, and asserts the returned `symbol/current_price` come from the cached payload:
```python
@pytest.mark.asyncio
@pytest.mark.unit
class TestFetchSectorPeersCache:
    async def test_target_served_from_integration_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from app.core.config import settings
        from app.services.naver_finance import peer_cache

        monkeypatch.setattr(settings, "naver_peer_cache_enabled", True)
        monkeypatch.setattr(settings, "naver_peer_fetch_concurrency", 5)

        class _FakeRedis:
            def __init__(self) -> None:
                self.store: dict[str, str] = {}

            async def get(self, key: str) -> str | None:
                return self.store.get(key)

            async def set(self, key: str, value: str, ex: int | None = None) -> None:
                self.store[key] = value

        fake = _FakeRedis()
        fake.store["naver_peer:integ:000100"] = json.dumps({
            "symbol": "000100", "name": "CachedTarget", "per": 7, "pbr": 0.9,
            "market_cap": 500, "current_price": 12345, "change_pct": 2.0,
            "industry_code": "123", "peers_raw": [{"itemCode": "000200"}],
        })

        async def fake_get_client() -> Any:
            return fake

        monkeypatch.setattr(peer_cache, "_get_redis_client", fake_get_client)

        async def fake_fetch_integration(
            code: str, _client: Any, request_timeout: float | None = None
        ) -> dict[str, Any]:
            if code == "000100":
                raise AssertionError("target must be served from cache, not fetched")
            return {
                "symbol": code, "name": "Peer", "per": 11, "pbr": 1.2,
                "market_cap": 900, "current_price": 40000, "change_pct": 0.5,
                "industry_code": "123", "peers_raw": [],
            }

        class FakeResponse:
            content = b"<html><head><title>x : Npay</title></head></html>"

        class FakeClient:
            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def get(self, url: str, params: Any = None) -> FakeResponse:
                return FakeResponse()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", lambda **_k: FakeClient())
        monkeypatch.setattr(
            naver_finance.valuation, "_fetch_integration", fake_fetch_integration
        )

        result = await naver_finance.fetch_sector_peers("000100", limit=1)

        assert result["name"] == "CachedTarget"
        assert result["current_price"] == 12345
        assert result["peers"][0]["symbol"] == "000200"
```

- [ ] **Run it — fails.** `uv run pytest tests/test_naver_finance.py -k "SectorPeersCache" -v`
  Expected: `AssertionError: target must be served from cache, not fetched` — `fetch_sector_peers` does not consult the cache yet.

- [ ] **Minimal impl — cache-aside wrapper + wiring in `valuation.py`.** Add `from app.services.naver_finance import peer_cache` (or lazy import to avoid a cycle — `peer_cache` imports only config + ohlcv_cache_common, so a top-level import is safe) and a wrapper above `fetch_sector_peers`:
```python
async def _fetch_integration_cached(
    code: str,
    client: httpx.AsyncClient,
    redis_client: Any = None,
    *,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """Cache-aside over _fetch_integration (ROB-688). Fail-open: any cache miss
    or Redis outage falls through to the live fetch; only non-degraded results
    (a resolved name) are written back."""
    cached = await peer_cache.get_cached_integration(redis_client, code)
    if cached is not None:
        return cached
    result = await _fetch_integration(code, client, request_timeout=request_timeout)
    if result.get("name"):
        await peer_cache.set_cached_integration(redis_client, code, result)
    return result
```
  In `fetch_sector_peers`, open the client and one Redis handle, then route target + peers through the wrapper:
```python
    redis_client = await peer_cache._get_redis_client()
    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=10) as client:
        target = await _fetch_integration_cached(code, client, redis_client)
        ...
        async def _safe_fetch(pc: str) -> dict[str, Any] | None:
            async with semaphore:
                try:
                    return await _fetch_integration_cached(
                        pc,
                        client,
                        redis_client,
                        request_timeout=settings.naver_peer_fetch_timeout_seconds,
                    )
                except Exception:
                    return None
```
  For the sector page, wrap `_fetch_sector_soup` + derivation in a cache-aside on `industry_code` so a hit skips both the HTTP scrape and re-parse:
```python
        industry_code = target.get("industry_code")
        sector_name = None
        cached_sector = (
            await peer_cache.get_cached_sector(redis_client, str(industry_code))
            if industry_code
            else None
        )
        if cached_sector is not None:
            sector_name = cached_sector.get("sector_name")
            extra_codes_cached = list(cached_sector.get("extra_codes", []))
            sector_soup = None  # already derived; no scrape needed
        else:
            extra_codes_cached = None
            sector_soup = (
                await _fetch_sector_soup(str(industry_code), client)
                if industry_code
                else None
            )
```
  Then, in the `< limit` branch, source extras from `extra_codes_cached` when present else `_parse_sector_stock_codes(sector_soup)`; after the peer gather, resolve `sector_name` from `sector_soup` (when not already cached) via `_parse_sector_name`, and write back `set_cached_sector(redis_client, str(industry_code), {"sector_name": sector_name, "extra_codes": extra_codes_used})`. **Keep the constraint invariant:** the sector name is still resolved on every fresh call (scrape) and only *reused* from cache — never nulled.

  > **Implementation note for the executor:** keep this sector-cache wiring minimal — the load-bearing win is the integration-bundle cache + Semaphore. If the sector-cache branching risks regressing `test_fetches_sector_page_once_for_codes_and_name`, it is acceptable to cache ONLY the integration bundle in this task and leave the sector page fetched-live-each-call (still one fetch), deferring sector-page caching to a follow-up — but the integration-bundle cache and the "sector name never nulled" invariant are required.

- [ ] **Run it — passes.** `uv run pytest tests/test_naver_finance.py -k "SectorPeersCache" -v` → passes.

- [ ] **Regression — full naver suite + cache suite green with cache disabled by default.** `uv run pytest tests/test_naver_finance.py tests/test_naver_peer_cache.py -v` → all pass. With `NAVER_PEER_CACHE_ENABLED=false` (conftest), `_get_redis_client()` returns `None`, so `_fetch_integration_cached(code, client, None, request_timeout=...)` skips the cache and fails open to `_fetch_integration(code, client, request_timeout=...)`. Note this now routes the **target** through `_fetch_integration_cached(..., request_timeout=None)` too, so the existing `test_fetches_sector_page_once_for_codes_and_name` relies on the Task 2 fake widening (a 2-arg fake would raise `TypeError` on the un-wrapped target). Behavior is otherwise identical to today for every existing sector-peers/valuation test.

- [ ] **Lint.** `make lint`.

- [ ] **Commit.** `git add -A && git commit -m "perf(ROB-688): short-TTL fail-open Redis cache for get_sector_peers integration+sector fetches"`

---

## Done criteria

- A KR `get_sector_peers` call with `limit=5` issues at most `naver_peer_fetch_concurrency` (≈5) concurrent peer fetches instead of the ~20-request burst; peers fast-fail at ≈5s while the target keeps its 10s budget; the common path (integration already returns ≥ `limit` peers) issues only `limit` peer fetches; and a repeat call for a recurring symbol within the TTL serves the integration bundle from Redis. The output contract (`symbol/name/sector/current_price/change_pct/per/pbr/market_cap/peers/comparison`) is unchanged, `sector` is never nulled on the common path, and every new path fails open to today's behavior when Redis is down or the cache is disabled.
- migration-0, no broker/order/watch mutation, `make lint` clean, `uv run pytest tests/test_naver_finance.py tests/test_naver_peer_cache.py -v` green.
