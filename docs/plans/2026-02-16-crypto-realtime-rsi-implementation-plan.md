# Crypto Realtime RSI Unification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce Upbit API fan-out while serving realtime crypto RSI (closed daily OHLCV + current ticker), and standardize outward RSI contract to `rsi` only.

**Architecture:** Add a short-lived process-local ticker cache at the Upbit service boundary (TTL=2s, in-flight dedupe), build a shared realtime RSI engine in market-data tooling, and route all crypto RSI call sites through the shared engine. Keep Upbit throttling at API-call boundaries only and bypass ticker cache for order execution.

**Tech Stack:** Python 3.11, FastAPI MCP tooling, asyncio, pandas, pytest, Upbit REST, existing Upbit rate limiter + Redis OHLCV cache.

---

### Task 1: Add Upbit ticker short TTL cache with bypass support

**Files:**
- Modify: `app/services/upbit.py`
- Modify: `tests/test_upbit_service.py`

**Step 1: Write the failing tests**

Add tests for:
- TTL hit returns cached values without raw API call.
- Partial cache hit fetches only missing symbols.
- In-flight dedupe merges concurrent identical requests into one raw call.
- `use_cache=False` always performs fresh API call.

```python
@pytest.mark.asyncio
async def test_fetch_multiple_current_prices_cache_hit_within_ttl(monkeypatch):
    ...
    first = await upbit.fetch_multiple_current_prices(["KRW-BTC"])
    second = await upbit.fetch_multiple_current_prices(["KRW-BTC"])
    assert first == second
    assert raw_call_count == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_upbit_service.py -k "cache_hit_within_ttl or partial_cache_hit or inflight_dedupe or bypass_cache" -v`  
Expected: FAIL (new cache behavior not implemented yet)

**Step 3: Write minimal implementation**

In `app/services/upbit.py`:
- Add process-local ticker cache state and lock.
- Add cached helper: `fetch_multiple_current_prices_cached(market_codes, ttl_seconds=2.0, use_cache=True)`.
- Keep API-bound throttling unchanged in `_request_json`.
- Route `fetch_multiple_current_prices(...)` through cached helper by default.
- Ensure raw path still available for bypass (via `use_cache=False`).

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_upbit_service.py -k "cache_hit_within_ttl or partial_cache_hit or inflight_dedupe or bypass_cache" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/upbit.py tests/test_upbit_service.py
git commit -m "Add 2s Upbit ticker memory cache with bypass and dedupe"
```

### Task 2: Build shared realtime crypto RSI engine (200 candles, batch ticker)

**Files:**
- Modify: `app/mcp_server/tooling/market_data_indicators.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Write the failing tests**

Add tests for:
- `compute_crypto_realtime_rsi_map` uses one batch ticker request for multiple symbols.
- RSI uses ticker-overridden last close when ticker exists.
- Symbols with 15+ closes compute RSI even when <200 candles.
- Symbols with <15 valid closes return `None`.

```python
@pytest.mark.asyncio
async def test_compute_crypto_realtime_rsi_map_allows_under_200_but_min_15(monkeypatch):
    ...
    result = await market_data_indicators.compute_crypto_realtime_rsi_map(["KRW-NEW"])
    assert result["KRW-NEW"] is not None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "realtime_rsi_map" -v`  
Expected: FAIL (function not implemented)

**Step 3: Write minimal implementation**

In `app/mcp_server/tooling/market_data_indicators.py`:
- Add helper to normalize crypto symbols.
- Add helper to apply realtime price to last close.
- Add `compute_crypto_realtime_rsi_map(symbols, count=200, use_ticker_cache=True)`.
- Fetch OHLCV per symbol via existing `_fetch_ohlcv_for_indicators(..., count=200)`.
- Fetch ticker once via `upbit_service.fetch_multiple_current_prices(..., use_cache=use_ticker_cache)`.
- Compute RSI(14) via `_calculate_rsi` and return flat map `{symbol: rsi_or_none}`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "realtime_rsi_map" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/market_data_indicators.py tests/test_mcp_server_tools.py
git commit -m "Add shared realtime crypto RSI engine using 200 candles"
```

### Task 3: Refactor crypto screening RSI path to shared engine and remove task semaphore

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screen_core.py`
- Modify: `tests/test_mcp_screen_stocks.py`

**Step 1: Write the failing tests**

Add/adjust tests for:
- `_screen_crypto(..., enrich_rsi=True)` calls shared realtime RSI engine once per batch.
- Returned rows use `rsi` only (no `rsi_14` in payload).
- Existing `sort_by="rsi"` behavior remains ascending/bucket-based.

```python
@pytest.mark.asyncio
async def test_screen_crypto_uses_batch_realtime_rsi_engine(monkeypatch):
    ...
    assert realtime_rsi_called_once
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_screen_stocks.py -k "batch_realtime_rsi_engine or sort_by_rsi" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

In `analysis_screen_core.py`:
- Replace per-item RSI enrichment logic with shared `compute_crypto_realtime_rsi_map`.
- Remove crypto task semaphore used only for RSI fan-out.
- Populate `item["rsi"]` + `rsi_bucket` from shared result.
- Keep diagnostics/warnings semantics intact.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_screen_stocks.py -k "batch_realtime_rsi_engine or sort_by_rsi" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_screen_core.py tests/test_mcp_screen_stocks.py
git commit -m "Refactor crypto screening RSI to shared realtime engine"
```

### Task 4: Refactor recommend_stocks crypto path and remove `rsi_14` contract

**Files:**
- Modify: `app/mcp_server/tooling/analysis_recommend.py`
- Modify: `tests/test_mcp_recommend.py`

**Step 1: Write the failing tests**

Add/adjust tests for:
- Recommendation payload exposes `rsi` only.
- Reason builder and sorting use `rsi`.
- Crypto enrichment does not write/read `rsi_14`.

```python
def test_recommend_crypto_payload_uses_rsi_only(...):
    ...
    assert "rsi" in recommendation
    assert "rsi_14" not in recommendation
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_recommend.py -k "rsi_only or crypto" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

In `analysis_recommend.py`:
- Replace `rsi_14` references with `rsi`.
- Update `_build_crypto_rsi_reason`, normalization, sorting keys, enrichment targets.
- Remove legacy `rsi_14` output fields in recommendation payload.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_recommend.py -k "rsi_only or crypto" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/analysis_recommend.py tests/test_mcp_recommend.py
git commit -m "Standardize crypto recommendation RSI contract to rsi"
```

### Task 5: Wire shared realtime RSI into indicators/portfolio/quotes, bypass cache in order execution

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/mcp_server/tooling/portfolio_dca_core.py`
- Modify: `app/mcp_server/tooling/order_execution.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Write the failing tests**

Add/adjust tests for:
- `get_indicators` crypto RSI path uses shared realtime RSI output.
- DCA summary returns `rsi` (not `rsi_14`) in outward payload.
- `_get_current_price_for_order` calls ticker with `use_cache=False`.

```python
@pytest.mark.asyncio
async def test_order_execution_bypasses_ticker_cache(monkeypatch):
    ...
    assert captured_use_cache is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "get_indicators and crypto and rsi" -v`  
Run: `uv run pytest tests/test_mcp_server_tools.py -k "dca and rsi" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- In `portfolio_holdings.py` and `market_data_quotes.py`: when `market_type == "crypto"` and RSI is requested, override RSI with shared realtime RSI result.
- In `portfolio_dca_core.py`: outward summary key becomes `rsi` (DB persistence field can remain unchanged in this task).
- In `order_execution.py`: call `fetch_multiple_current_prices(..., use_cache=False)` for crypto current-price lookup.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_server_tools.py -k "get_indicators and crypto and rsi or dca and rsi" -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/market_data_quotes.py app/mcp_server/tooling/portfolio_dca_core.py app/mcp_server/tooling/order_execution.py tests/test_mcp_server_tools.py
git commit -m "Apply shared realtime RSI across portfolio/quotes and bypass order cache"
```

### Task 6: Contract sweep, docs update, and verification

**Files:**
- Modify: `app/mcp_server/scoring.py`
- Modify: `app/mcp_server/README.md`
- Modify: `tests/test_mcp_recommend.py`
- Modify: `tests/test_mcp_server_tools.py`

**Step 1: Write the failing tests**

Add/adjust tests so composite scoring/reasoning paths no longer depend on `rsi_14` input fallback.

```python
def test_calc_composite_score_uses_rsi_field_only():
    ...
    assert score is not None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_recommend.py tests/test_mcp_server_tools.py -k "rsi_14 or rsi_field_only" -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- Remove `rsi_14` fallback references in scoring/reason generation where the runtime payload already guarantees `rsi`.
- Update MCP README response examples to `rsi` key only.
- Keep unrelated storage schema fields out of scope for this change.

**Step 4: Run full verification**

Run:
- `uv run pytest tests/test_upbit_service.py -v`
- `uv run pytest tests/test_mcp_screen_stocks.py -v`
- `uv run pytest tests/test_mcp_recommend.py -v`
- `uv run pytest tests/test_mcp_server_tools.py -v`
- `make lint`

Expected: All PASS

**Step 5: Commit**

```bash
git add app/mcp_server/scoring.py app/mcp_server/README.md tests/test_mcp_recommend.py tests/test_mcp_server_tools.py
git commit -m "Finalize rsi-only contract and verification updates"
```

### Implementation Notes

- Use @test-driven-development before each task implementation step.
- Use @verification-before-completion before claiming final completion.
- Keep commits frequent and scoped to each task.
- Do not introduce Redis-based ticker cache in this scope (YAGNI).
