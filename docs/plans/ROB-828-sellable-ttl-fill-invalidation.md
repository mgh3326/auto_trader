# ROB-828 Sellable TTL + Event Invalidation Implementation Plan

> **For agentic workers:** Execute inline with strict RED → GREEN cycles. Do not write production code for a behavior until its failing test has been observed.

**Goal:** Make Toss sellable-quantity caching effective for real MCP call intervals while preserving display correctness by invalidating the affected symbol whenever a fill or sell-order mutation changes sellable quantity.

**Architecture:** Replace the per-process in-memory value store behind `TossSellableCache` with a fail-open Redis cache-aside using `toss:sellable:v1:{SYMBOL}` keys and a 600-second default TTL. Portfolio reads use one `MGET` and one pipelined write for all holdings; the ROB-757 scheduled poller deletes keys for newly booked fills, and successful sell place/cancel/modify broker mutations delete their symbol immediately. API home readers and MCP holdings continue to receive the same portfolio snapshot contract and now share warm Redis values across processes.

Each symbol also has a Redis generation key. Miss reads capture value+generation in the same `MGET`; writes use `WATCH`/`MULTI` and store a fetched value only if the generation is unchanged. Invalidation atomically increments the generation and deletes the value, preventing an in-flight pre-event broker fetch from restoring stale data after `DEL`.

**Tech Stack:** Python 3.13, redis-py asyncio, fakeredis, FastAPI/TaskIQ/FastMCP, pytest, Ruff, ty.

## Global Constraints

- `TOSS_SELLABLE_CACHE_TTL_SECONDS` default changes from `45.0` to `600.0`; the existing environment override remains the rollback control.
- `TOSS_SELLABLE_CACHE_ENABLED=false` remains a complete cache kill switch.
- `fresh_sellable=True` still bypasses the cache and performs live per-symbol broker reads.
- `need_sellable=False` still skips both Redis and Toss sellable-quantity reads.
- Only successful sellable responses are cached; degraded/error results are never cached.
- Redis `MGET`, pipeline write, and `DEL` failures are log-and-continue. Reads fail open as misses; writes and invalidations never fail portfolio reads, order mutations, or reconciliation.
- A cache miss that started before an invalidation must not write its stale result after that invalidation; per-symbol generations enforce this race boundary.
- A successful broker sell place, sell cancel, or sell modify invalidates after the broker mutation succeeds, even if later ledger recording fails.
- Buy-order mutations do not invalidate sellable because they do not change sellable quantity.
- Order validation, broker payloads, mutation gates, ledger behavior, and response contracts do not change.
- migration-0: no model, schema, or Alembic changes.
- Single Toss account assumption is inherited from ROB-701; cache keys therefore do not include account sequence.

## Root Cause and ROB-757 Connection Point

The 45-second in-memory cache only helps adjacent page loads. MCP calls arrive minutes apart, so four of five observed calls expired before reuse and repeated the ~7.2-second sellable fanout. Extending an in-memory cache alone is also insufficient because production runs the TaskIQ worker, API home reader, and MCP servers in separate processes.

ROB-757 discovery (`TossFillPollerService.discover_external_orders`) only seeds missing orders; it does not prove a new fill. The confirmed fill boundary is:

```text
toss_live_poll_fills_periodic
  -> toss_reconcile_orders_impl(dry_run=False)
  -> _reconcile_one_toss_row
  -> delta = broker_cumulative_filled_qty - already_booked_qty
  -> action == "booked" after execution-ledger upsert + ledger outcome commit
```

Therefore `app/tasks/toss_live_reconcile_tasks.py` will collect unique `symbol` values from reconciliation outcomes whose `action` is `booked`, then issue a single Redis `DEL key...` through the cache port. `noop_already_booked`, pending, dry-run, transient, and anomaly outcomes do not invalidate.

Sellable changes at four event boundaries, all covered by this plan:

1. Confirmed fill: ROB-757 poller reconciliation deletes the filled symbol.
2. Resting sell submission: successful Toss `place_order` deletes the sell symbol.
3. Sell cancellation: successful Toss `cancel_order` deletes the original sell symbol.
4. Sell modification: successful Toss `modify_order` deletes the original sell symbol.

## Rejected Alternatives

- **Process-local `invalidate(symbol)` only:** rejected because worker invalidation cannot reach API/MCP process caches.
- **Redis pub/sub over local caches:** rejected because it adds subscriber lifecycle to every runtime and can lose messages; Redis cache-aside gives direct cross-process key deletion without subscribers.
- **Long TTL without event invalidation:** rejected because resting sell place/cancel/modify changes sellable before any fill and could otherwise display a larger stale sellable quantity for ten minutes.

## File Map

| File | Responsibility |
| --- | --- |
| `app/core/config.py` | Change the default TTL to 600 seconds. |
| `app/services/toss_sellable_cache.py` | Redis-backed cache port, key normalization, batched read/write/delete, fail-open logging, shared instance. |
| `app/services/toss_portfolio_service.py` | Use the cache port's batched methods while preserving snapshot behavior and successful-only writes. |
| `app/tasks/toss_live_reconcile_tasks.py` | Invalidate unique symbols from newly booked ROB-757 fill outcomes. |
| `app/mcp_server/tooling/orders_toss_variants.py` | Invalidate after successful broker sell place/cancel/modify mutations. |
| `app/mcp_server/README.md` | Record the 600-second shared Redis cache and explicit fresh bypass/event invalidation behavior. |
| `tests/test_toss_sellable_cache.py` | Cache port, TTL, MGET/pipeline, per-symbol invalidation, and fail-open tests using fakeredis. |
| `tests/test_toss_portfolio_service.py` | End-to-end cache-aside regression: invalidate one symbol, refetch only it, retain other cached symbols; preserve error and skip semantics. |
| `tests/tasks/test_toss_live_reconcile_tasks.py` | ROB-757 booked-outcome-to-invalidation wiring and deduplication. |
| `tests/test_mcp_toss_order_variants.py` | Successful sell place/cancel/modify invalidation and non-success/buy invariants. |

---

### Task 1: Redis Cache Port and 600-Second Default

**Files:**
- Modify: `tests/test_toss_sellable_cache.py`
- Modify: `app/core/config.py`
- Modify: `app/services/toss_sellable_cache.py`

**Interfaces:**
- Preserve: `TossSellableCache.get(symbol)`, `put(symbol, value)`, `clear()`, `get_shared_sellable_cache()`, and `reset_shared_sellable_cache()` names for port compatibility.
- Add: `await TossSellableCache.get_many(symbols) -> list[Decimal | None]`.
- Add: `await TossSellableCache.put_many(values) -> None`.
- Add: `await TossSellableCache.invalidate(symbol) -> None` and `invalidate_many(symbols) -> None`.
- Redis key: `toss:sellable:v1:{symbol.strip().upper()}`.

- [ ] **Step 1: Write failing backend tests first**

  Add fakeredis-backed async tests proving:

  ```python
  values = await cache.get_many(["AAA", "BBB"])
  assert values == [Decimal("3"), Decimal("5")]
  assert redis_client.mget.await_count == 1

  await cache.invalidate("AAA")
  assert await cache.get("AAA") is None
  assert await cache.get("BBB") == Decimal("5")
  ```

  Also change the settings default assertion to `600.0`, verify the env override remains effective, and add raising Redis doubles for MGET, pipeline execute, and DEL to prove miss/log-and-continue behavior.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest -q tests/test_toss_sellable_cache.py`

  Expected: FAIL because the default remains `45.0`, the cache has no Redis injection or batch API, and `invalidate` is missing.

- [ ] **Step 3: Implement the minimal Redis cache port**

  Use a lazy/shared `redis.asyncio.Redis` client configured from `settings.get_redis_url()`, `decode_responses=True`, and existing Redis timeouts. `get_many` must call one `MGET`; `put_many` must pipeline `SET ... PX=<ttl_ms>` calls; `invalidate_many` must call one multi-key `DEL`. Parse malformed/non-decimal values as misses. Catch broad Redis/backend exceptions at the port boundary and log without raising.

- [ ] **Step 4: Run GREEN**

  Run: `uv run pytest -q tests/test_toss_sellable_cache.py`

  Expected: all cache tests pass.

### Task 2: Portfolio Batch Cache-Aside and Exact-Symbol Refetch

**Files:**
- Modify: `tests/test_toss_portfolio_service.py`
- Modify: `app/services/toss_portfolio_service.py`

**Interfaces:**
- Consume: `await sellable_cache.get_many(symbols)` and `await sellable_cache.put_many(successful_values)`.
- Preserve: `fetch_toss_portfolio_snapshot(...)` arguments and returned `TossPortfolioSnapshot`.

- [ ] **Step 1: Write the required failing integration test**

  Warm fakeredis with `AAA=3` and `BBB=5`, deliver the invalidation by calling the real cache port for `AAA`, then fetch a two-position portfolio. Assert:

  ```python
  assert client.sellable_symbols == ["AAA"]
  assert snapshot.positions[0].sellable_quantity == Decimal("2")  # refetched
  assert snapshot.positions[1].sellable_quantity == Decimal("5")  # Redis hit
  assert await cache.get("AAA") == Decimal("2")
  assert await cache.get("BBB") == Decimal("5")
  ```

  Keep/add assertions that a failed broker result is not cached, `need_sellable=False` performs no Redis or sellable call, and the fresh path still passes `sellable_cache=None` from MCP.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest -q tests/test_toss_portfolio_service.py -k 'cache or sellable'`

  Expected: FAIL because the existing synchronous in-memory cache cannot use Redis/MGET.

- [ ] **Step 3: Implement one-batch cache reads and writes**

  Replace the per-item cache lookup with one `get_many` call. Preserve miss-index ordering and the existing concurrent Toss fanout only for misses. Build one mapping of successful results and call `put_many` once. Do not change the no-cache, `need_sellable=False`, cash, error, or position-building branches.

- [ ] **Step 4: Run GREEN and home/MCP regressions**

  Run:

  ```bash
  uv run pytest -q tests/test_toss_portfolio_service.py \
    tests/mcp_server/tooling/test_toss_sellable_need_flag.py \
    tests/test_mcp_portfolio_tools.py
  ```

  Expected: all pass, proving both API home reader and MCP paths preserve their contracts.

### Task 3: ROB-757 Fill Outcome Invalidation

**Files:**
- Modify: `tests/tasks/test_toss_live_reconcile_tasks.py`
- Modify: `app/tasks/toss_live_reconcile_tasks.py`

**Interfaces:**
- Consume reconciliation outcomes from `result["reconciled"]`.
- Invalidate only outcomes where `action == "booked"` and `symbol` is non-empty.

- [ ] **Step 1: Write the failing poller wiring test**

  Make the mocked reconcile kernel return duplicate booked outcomes for `AAA`, a booked `BBB`, and non-booked `CCC`. Assert the real cache invalidation boundary receives exactly `AAA` and `BBB` once each, and that the task still returns reconciliation success.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest -q tests/tasks/test_toss_live_reconcile_tasks.py`

  Expected: FAIL because the task does not inspect booked outcomes or invalidate cache keys.

- [ ] **Step 3: Implement post-reconcile batched invalidation**

  After `toss_reconcile_orders_impl(dry_run=False, ...)` returns, collect and sort/dedupe booked symbols and call `invalidate_many` once. The cache port absorbs DEL failures, so reconciliation remains the task's primary success/failure contract.

- [ ] **Step 4: Run GREEN**

  Run: `uv run pytest -q tests/tasks/test_toss_live_reconcile_tasks.py tests/services/test_toss_fill_poller_service.py`

  Expected: all pass.

### Task 4: Sell Place/Cancel/Modify Invalidation

**Files:**
- Modify: `tests/test_mcp_toss_order_variants.py`
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`

**Interfaces:**
- Consume: the shared cache invalidation helper only after `client.place_order`, `client.cancel_order`, or `client.modify_order` succeeds.
- Preserve all mutation payloads, validation gates, ledger calls, and responses.

- [ ] **Step 1: Write failing mutation-boundary tests**

  Add tests that warm a fakeredis key, execute each successful broker mutation, and assert the key is gone for:

  - live `toss_place_order(..., side="sell")`;
  - live sell `toss_cancel_order(...)`;
  - live sell `toss_modify_order(...)`.

  Also assert buy place does not invalidate, dry-run does not invalidate, and a broker exception leaves the key untouched. Include a ledger-failure-after-broker-success case proving invalidation already happened.

- [ ] **Step 2: Run RED**

  Run: `uv run pytest -q tests/test_mcp_toss_order_variants.py -k 'sellable_cache'`

  Expected: FAIL because successful mutations do not delete Redis keys.

- [ ] **Step 3: Add minimal post-broker-success invalidation**

  Immediately after each successful broker mutation response and before ledger recording, call the fail-open cache invalidation helper when the order side is sell. Do not add invalidation to preview, validation failure, broker failure, or buy-order paths.

- [ ] **Step 4: Run GREEN**

  Run: `uv run pytest -q tests/test_mcp_toss_order_variants.py -k 'sellable_cache'`

  Expected: all targeted mutation tests pass.

### Task 5: Documentation and Full Verification

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Update runtime documentation**

  Document that default holdings reads share a Redis-backed 600-second cache across API/MCP processes, successful values only are cached, `fresh_sellable=True` bypasses it, and confirmed fills plus successful sell place/cancel/modify invalidate only the affected symbol.

- [ ] **Step 2: Run related suites**

  Run:

  ```bash
  uv run pytest -q \
    tests/test_toss_sellable_cache.py \
    tests/test_toss_portfolio_service.py \
    tests/services/test_toss_fill_poller_service.py \
    tests/tasks/test_toss_live_reconcile_tasks.py \
    tests/mcp_server/tooling/test_toss_sellable_need_flag.py \
    tests/test_mcp_portfolio_tools.py \
    tests/test_mcp_toss_order_variants.py
  ```

  Expected: all pass.

- [ ] **Step 3: Run repository quality gates**

  Run: `make lint`

  Expected: Ruff format/check and ty checks pass.

- [ ] **Step 4: Review the final diff for scope and safety**

  Confirm no migration, model, schema, buying-power, KIS/Upbit, or order validation/payload changes. Confirm every Redis operation is fail-open and all changed behavior has observed RED evidence.

- [ ] **Step 5: Commit, push, and create a PR without merging**

  Use the repository ship workflow, include `ROB-828` in the commit/PR title, link the Linear issue, report verification commands, and stop after PR creation.

## Plan Self-Review

- Coverage: all five approval conditions, four sellable-changing events, kill switch, fresh/no-sellable paths, home+MCP sharing, migration-0, and PR-only delivery are mapped to tasks.
- Ambiguity resolved: `get/put` remain supported port methods; batched `get_many/put_many/invalidate_many` are added because a literal per-symbol Redis implementation would violate the MGET requirement. Only the internal portfolio cache access changes; home reader and MCP caller contracts do not.
- Scope: no pub/sub subscriber lifecycle, no unrelated refactor, no order behavior change beyond successful mutation cache invalidation.
- Placeholders: none.
