# ROB-827 VTS Token Cache Implementation Plan

> **For agentic workers:** Execute this plan inline with strict red-green-refactor TDD. Every production change requires a test that was observed failing for the intended reason first.

**Goal:** Make KIS VTS token acquisition complete once, persist in Redis, and remain single-flight across MCP-created clients, while isolating cache and lock keys by VTS host and appkey without changing live KIS token behavior or any order request path.

**Architecture:** Keep `BaseKISClient._ensure_token()` as the only token-ensure seam and keep `RedisTokenManager` as the cache/lock implementation. Mock clients receive a process-shared `RedisTokenManager` selected by a non-secret namespace derived from the normalized VTS host plus a SHA-256 appkey fingerprint. The VTS token POST alone gets a 10-second timeout so the observed 4-6 second issuance can finish and be cached, and VTS lock contenders wait 11 seconds so they can observe that result instead of failing at the legacy 3-second live budget. Live keeps its existing singleton, Redis keys, 5-second request timeout, and 3-second lock-wait budget.

**Tech Stack:** Python 3.13, asyncio, httpx, Redis, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- Work only in `/Users/mgh3326/work/auto_trader.rob-827`; do not modify canonical `/Users/mgh3326/work/auto_trader`.
- Write and observe failing tests before production changes.
- Preserve the live `redis_token_manager` singleton and its keys (`kis:access_token`, `kis:token:lock`).
- Never place the raw appkey or appsecret in a Redis key, log, assertion failure, or PR text.
- Namespace VTS cache and lock keys by both normalized host and appkey fingerprint.
- Keep order dispatch, order TR IDs, order payloads, and order endpoint timeouts unchanged.
- No schema or data migration (`migration-0`).
- KIS token issuance is rate-limited, so both Redis reuse and single-flight are correctness protections as well as latency optimizations.

---

## Root Cause Confirmation

### MCP-to-token call path

1. `get_available_capital_impl()` delegates to `get_cash_balance_impl()` at `app/mcp_server/tooling/portfolio_cash.py:347-364`.
2. The KIS cash branch creates a fresh client for every tool call at `app/mcp_server/tooling/portfolio_cash.py:39-42` and `:206-219`; mock mode constructs `KISClient(is_mock=True)`.
3. `get_holdings` follows the same pattern at `app/mcp_server/tooling/portfolio_holdings.py:293-304`, then runs KR and US balance reads at `:305-345`.
4. Account reads call `_ensure_token()` before the broker request (`app/services/brokers/kis/account.py:221`, `:386`, and `:531`).
5. `_ensure_token()` checks Redis and delegates a miss to `refresh_token_with_lock()` at `app/services/brokers/kis/base.py:316-334`.
6. `refresh_token_with_lock()` performs cache rechecks, obtains the Redis lock, rechecks under the lock, fetches once, and saves the result at `app/services/redis_token_manager.py:243-307`.

### Confirmed defect

The mock path is not completely outside Redis, but its current integration is incomplete:

- Every `KISClient(is_mock=True)` constructs a new manager at `app/services/brokers/kis/client.py:104-110`, discarding the manager's process-local token cache and local coordination on every MCP tool call.
- Every VTS deployment/appkey uses the same `kis_mock:access_token` and `kis_mock:token:lock` keys because `RedisTokenManager.__init__()` only interpolates the literal namespace at `app/services/redis_token_manager.py:16-20`. This violates the required host/appkey boundary and permits stale or cross-credential tokens to trigger invalid-token clearing/reissuance.
- The direct OAuth POST uses a hard-coded 5-second timeout at `app/services/brokers/kis/base.py:293-302`. Sentry trace `fd0bfb06bd084dac83573efec7ad225b` showed: Redis miss, lock acquired, VTS POST lasting 6.675 seconds, lock released without a save, then the same `get_holdings` call repeated the miss and made another 5.048-second VTS POST. Because both token attempts fail before `save_token()` (`app/services/redis_token_manager.py:294-300`), later tools correctly see another miss and reissue.
- Pre-implementation review exposed a second timing defect at `app/services/redis_token_manager.py:278-287`: a contender waited only about 3 seconds for the lock owner. Once the VTS request budget became 10 seconds, a realistic 4-6 second issuance would succeed for the owner but still raise `RuntimeError` for the waiter. A separate-manager/shared-Redis test reproduced this with a 3.4-second POST before the wait-budget fix.
- The positive control is Sentry trace `daef06d001e144c0a98be16908ec3965`: a 395ms VTS token POST succeeded, and the immediately following `get_holdings` trace had no token POST. This proves the existing Redis hit path works after a successful save and isolates the repeated issuance to cold-miss completion plus inadequate namespace ownership.

**Root cause hypothesis verified:** VTS issuance frequently exceeds the live-oriented 5-second token timeout, so no token reaches Redis; fresh per-tool mock managers then retry the cold path. The only mock Redis keys are global literals rather than host/appkey-scoped keys, so the existing integration also cannot safely own a cached VTS token across credential rotations or parallel deployments.

## Considered Approaches

### A. Credential-scoped shared VTS manager plus VTS-only token timeout (selected)

- Derive the manager namespace from `urlsplit(base_url).netloc.lower()` and the first 16 hex characters of `sha256(app_key)`.
- Cache one manager per derived namespace in the process; Redis remains the cross-process source of truth and distributed lock.
- Use 10 seconds only for mock OAuth issuance; live remains 5 seconds.
- Give only VTS managers an 11-second contender wait; the live manager retains the existing 3-second default.
- Covers the observed failure, the requested Redis/single-flight behavior, and host/appkey isolation with a narrow diff.

### B. Change only `kis_mock` to a host/appkey-derived namespace

- Fixes unsafe key collision.
- Does not let the observed 4-6 second cold issuance finish, so the repeated miss pattern remains.

### C. Add a cache in each MCP tool handler

- Duplicates token ownership across `get_holdings`, `get_cash_balance`, `get_available_capital`, home readers, and order helpers.
- Bypasses the existing Redis lock and increases the chance of live/mock routing drift.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `CHANGELOG.md` | Modify | Record the ROB-827 migration-0 operator-visible behavior change. |
| `docs/plans/ROB-827-vts-token-cache.md` | Create | Root-cause record, design, TDD plan, and verification checklist. |
| `tests/test_kis_vts_token_cache.py` | Create | Cache-hit, expiry refresh, single-flight, host/appkey key isolation, and live-timeout invariants. |
| `app/services/redis_token_manager.py` | Modify | Build a non-secret VTS namespace, return one process-shared manager per host/appkey scope, and configure its contender wait beyond the VTS request budget. Existing live singleton and defaults stay untouched. |
| `app/services/brokers/kis/client.py` | Modify | Route `KISClient(is_mock=True)` through the scoped manager factory and select the VTS-only OAuth timeout. |
| `app/services/invest_home_readers.py` | Modify | Give `SafeKISMockClient` the same scoped manager and VTS-only OAuth timeout, preventing a second mock token ownership scheme. |
| `app/services/brokers/kis/base.py` | Modify | Replace the two literal token timeout values with `_token_request_timeout()`; default remains 5 seconds. |

No MCP request/response contract changes, so `app/mcp_server/README.md` does not require an interface update.

---

### Task 1: Lock the VTS cache contract with failing tests

**Files:**
- Create: `tests/test_kis_vts_token_cache.py`
- Read only: `app/services/redis_token_manager.py:13-29,131-307`
- Read only: `app/services/brokers/kis/base.py:266-334`
- Read only: `app/services/brokers/kis/client.py:43-120`

**Interfaces tested:**
- Existing `KISClient(*, is_mock: bool = False)` constructor.
- Existing `BaseKISClient._ensure_token() -> None` and `_fetch_token() -> tuple[str, int]`.
- Redis protocol used by `RedisTokenManager`: async `get`, `set`, `delete`, and `execute_command`.

- [x] **Step 1: Create a stateful fake Redis at the same abstraction as production Redis.**

  Implement `_FakeRedis` with a `values: dict[str, str]`, atomic `set(..., nx=True)`, `get`, `delete`, and the lock-release `execute_command("EVAL", ...)` behavior. It must preserve the cache/lock side effects the tests depend on rather than mocking `get_token()` or `refresh_token_with_lock()`.

- [x] **Step 2: Add a cache-hit test that proves token POST count is zero.**

  Configure a unique mock host/appkey, construct `KISClient(is_mock=True)`, assert its token key equals `kis_mock:{host}:{sha256(appkey)[:16]}:access_token`, preload valid token JSON at that key, attach `_FakeRedis`, and call `await client._ensure_token()`. Assert the mock HTTP client and its `post` method were never awaited and `settings.kis_mock_access_token` equals the cached token.

- [x] **Step 3: Add an expiry test that proves exactly one reissue and Redis replacement.**

  Preload expired JSON at the expected scoped key, return `{"access_token": "fresh-vts-token", "expires_in": 7200}` from the fake HTTP response, call `_ensure_token()`, and assert one POST to `https://{mock-host}/oauth2/token`, one new token stored at the scoped key, and no write to `kis_mock:access_token`.

- [x] **Step 4: Add the live/mock and host/appkey separation test.**

  Construct live, mock-A, mock-B (same host, different appkey), and mock-C (different host, same appkey) clients. Assert the live key remains exactly `kis:access_token`; all mock keys differ from live and from each other; raw appkeys are absent from all keys. Also assert two clients with identical mock host/appkey reuse the same manager object.

- [x] **Step 5: Add a concurrent single-flight test.**

  Give two same-scope mock clients the same `_FakeRedis`, concurrently call both `_ensure_token()` methods, and assert the combined OAuth POST count is one and both clients receive the same token. This exercises the real Redis lock/cache code rather than asserting only on a mocked manager.

- [x] **Step 6: Add timeout invariants.**

  Call `_fetch_token()` on a mock and a live client with fake HTTP. Assert mock `_ensure_client` and `post` receive `10.0`, while live receives `5.0`. Do not invoke any order method.

- [x] **Step 7: Run RED and inspect the failure reason.**

  Run:

  ```bash
  uv run pytest tests/test_kis_vts_token_cache.py -v
  ```

  Expected current-code failures:

  - mock key is `kis_mock:access_token`, not host/appkey-scoped;
  - identical mock clients own different manager objects;
  - mock token POST timeout is 5 seconds, not 10 seconds.

  Existing cache-hit/expiry mechanics may reach later assertions only after the namespace assertion is satisfied. Collection errors, real network calls, or failures unrelated to these missing behaviors do not count as RED and must be corrected before implementation.

### Task 2: Add host/appkey-scoped VTS Redis manager ownership

**Files:**
- Modify: `app/services/redis_token_manager.py:1-29,326-327`
- Modify: `app/services/brokers/kis/client.py:10-12,104-110`
- Modify: `app/services/invest_home_readers.py:960-977`
- Test: `tests/test_kis_vts_token_cache.py`

**Interfaces produced:**
- `get_kis_mock_token_manager(*, base_url: str, app_key: str) -> RedisTokenManager`.
- Redis namespace `kis_mock:{normalized_netloc}:{sha256(app_key)[:16]}`; `RedisTokenManager` appends `:access_token` and `:token:lock` exactly as today.

- [x] **Step 1: Implement deterministic non-secret namespace construction.**

  In `redis_token_manager.py`, use `urllib.parse.urlsplit` to normalize the lower-case network location and `hashlib.sha256` to fingerprint the appkey. Preserve construction-only test compatibility by hashing an empty appkey too; existing MCP config validation remains responsible for blocking a network call with missing credentials. Reject only an empty normalized host. Preserve the module-level live `redis_token_manager = RedisTokenManager()` and all of its defaults.

- [x] **Step 2: Implement a process-shared mock manager factory.**

  Cache `RedisTokenManager` instances by the derived namespace using `functools.cache`. Same host/appkey returns the same object/local cache; different scopes get different Redis cache and lock keys. The raw appkey must not appear in the cache key or logs.

- [x] **Step 3: Wire both mock clients to the factory.**

  Replace `RedisTokenManager("kis_mock")` in `KISClient.__init__()` and `SafeKISMockClient.__init__()` with `get_kis_mock_token_manager(base_url=..., app_key=...)`. Live construction must continue to inherit the unchanged global `redis_token_manager` from `BaseKISClient.__init__()`.

- [x] **Step 4: Run the focused tests.**

  ```bash
  uv run pytest tests/test_kis_vts_token_cache.py tests/test_kis_mock_routing.py tests/test_redis_token_manager.py -v
  ```

  Expected: namespace, shared-manager, cache-hit, expiry, and concurrency tests pass; timeout test remains RED until Task 3.

### Task 3: Let slow VTS token issuance finish without changing live

**Files:**
- Modify: `app/services/brokers/kis/base.py:266-302`
- Modify: `app/services/brokers/kis/client.py:104-120`
- Modify: `app/services/invest_home_readers.py:960-987`
- Test: `tests/test_kis_vts_token_cache.py`

**Interfaces produced:**
- `BaseKISClient._token_request_timeout() -> float`, default `5.0`.
- Mock overrides return `10.0`; no public API changes.

- [x] **Step 1: Extract the existing live timeout behind a protected method.**

  Add `_token_request_timeout()` to `BaseKISClient` returning `5.0`. In `_fetch_token()`, compute it once and pass it to both `_ensure_client(timeout=...)` and `cli.post(..., timeout=...)`. No retry, payload, breaker, or response parsing code changes.

- [x] **Step 2: Override only mock clients.**

  `KISClient._token_request_timeout()` returns `10.0` when `_is_mock_client` is true and delegates to `super()` otherwise. `SafeKISMockClient._token_request_timeout()` returns `10.0`. The data/order request timeouts in account and order clients remain untouched.

- [x] **Step 3: Extend only the VTS lock-contender budget.**

  Add a configurable wait budget to `RedisTokenManager` with the existing 3-second behavior as its default. Configure host/appkey-scoped VTS managers with 11 seconds so a contender outlives the 10-second VTS request. Prove it with two distinct managers sharing fake Redis and a 3.4-second token POST: both calls complete and the combined POST count is one. Live remains at 3 seconds.

- [x] **Step 4: Run GREEN.**

  ```bash
  uv run pytest tests/test_kis_vts_token_cache.py tests/test_kis_mock_routing.py tests/test_redis_token_manager.py tests/test_kis_token_circuit_breaker.py tests/test_kis_token_circuit_open_propagation.py -v
  ```

  Expected: all selected tests pass, including live 5-second and VTS 10-second assertions.

### Task 4: Verify relevant suites, lint, and scope

**Files:**
- Verify only; production edits are complete.

- [x] **Step 1: Run KIS and MCP account-read regression suites.**

  ```bash
  uv run pytest \
    tests/test_kis_vts_token_cache.py \
    tests/test_kis_mock_routing.py \
    tests/test_kis_settings_view_isolation.py \
    tests/test_redis_token_manager.py \
    tests/test_kis_token_circuit_breaker.py \
    tests/test_kis_token_circuit_open_propagation.py \
    tests/test_mcp_available_capital.py \
    tests/test_mcp_portfolio_tools.py \
    tests/test_mcp_account_modes.py \
    -v
  ```

- [x] **Step 2: Run the complete unit suite.**

  ```bash
  make test-unit
  ```

- [x] **Step 3: Run lint and type checks through the project gate.**

  ```bash
  make lint
  ```

- [x] **Step 4: Confirm migration and order-path boundaries.**

  ```bash
  git diff --name-only origin/main...HEAD
  git diff origin/main...HEAD -- alembic app/services/brokers/kis/domestic_orders.py app/services/brokers/kis/overseas_orders.py
  ```

  Expected: no Alembic files and no order-client changes.

- [x] **Step 5: Review the final diff against every global constraint.**

  Confirm cached hit = zero POST, expired entry = one POST then Redis replacement, concurrent cold starts = one POST, same VTS scope = one manager, different host/appkey = different cache+lock keys, live keys/timeouts unchanged, and no credential values in source/test output.

### Task 5: Commit, push, and create the PR without merging

**Files:**
- Stage only the plan, focused tests, and token-management implementation files.

- [x] **Step 1: Commit one coherent fix after fresh verification.**

  ```bash
  git add \
    CHANGELOG.md \
    docs/plans/ROB-827-vts-token-cache.md \
    tests/test_kis_vts_token_cache.py \
    app/services/redis_token_manager.py \
    app/services/brokers/kis/base.py \
    app/services/brokers/kis/client.py \
    app/services/invest_home_readers.py
  git commit -m "fix(ROB-827): cache VTS tokens by credential scope"
  ```

- [x] **Step 2: Push `rob-827` and create a PR targeting `main`.**

  The PR body must include:

  - root cause: VTS 5-second OAuth timeout prevented Redis save, plus literal `kis_mock` namespace and per-client manager ownership;
  - fix: host/appkey-hashed Redis cache+lock namespace, shared manager, VTS-only 10-second token timeout and 11-second contender wait;
  - proof: exact RED output, focused GREEN counts, complete unit-suite result, and `make lint` result;
  - safety: live token keys/5-second timeout unchanged, order files unchanged, migration-0;
  - `ROB-827` reference.

  Do not merge the PR.

## Self-Review

- Spec coverage: cache-hit zero POST, expiry reissue, live/mock key separation, host/appkey separation, single-flight, rate-limit rationale, live/order immutability, migration-0, full relevant tests, lint, and PR-only delivery are each mapped to explicit steps.
- Placeholder scan: no implementation placeholders remain.
- Type consistency: the manager factory returns the existing `RedisTokenManager`; `_ensure_token()` and `_fetch_token()` signatures stay unchanged; the new timeout seam returns `float` in base and both mock clients.
- Scope: one token-management subsystem, two existing mock client adapters, one focused test module, and this plan. No MCP schema, database, or order execution change.
