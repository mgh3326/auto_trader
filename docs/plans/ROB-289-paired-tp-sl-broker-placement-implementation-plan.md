# ROB-289 — Paired TP/SL Stop-Limit Broker Placement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` for the **implementation PR that follows this plan-only PR.** This PR ships only the plan document; implementation begins after plan review/merge.
>
> AOE_STATUS: plan_ready
> AOE_ISSUE: ROB-289
> AOE_ROLE: planner (this PR) → implementer (next PR)
> AOE_NEXT: open implementation PR on the same `rob-289` branch after plan review/merge; do NOT close ROB-289 with this plan-only PR (`Refs ROB-289`, not `Closes`).
>
> **Safety-critical reminder:** This is the first PR in the project to introduce **broker-mutation** code paths against the testnet — `place_stop_limit_order` and `place_stop_market_order` will send signed write requests to `testnet.binance.vision`. Every test layer continues to prove no live host is reachable and `dry_run=True` remains the default. If any test in the §Safety boundaries section is weakened during implementation, stop and ask.

**Goal:** Replace the synthetic cancel-and-close exit path in the testnet scalper with real **paired TP/SL stop orders** placed at the testnet broker after entry fill. The ledger already supports the paired representation (two rows linked by `parent_client_order_id`, lifecycle `tp_sl_armed → tp_sl_triggered`) — ROB-289 wires the actual broker placement + opposite-leg cancellation on trigger, plus the broker-reject fallback that keeps the existing cancel-and-close as a safety net.

**Architecture:** Two new methods on `BinanceTestnetExecutionClient` (`place_stop_limit_order`, `place_stop_market_order`) that share the existing signed transport, host allowlist, HMAC chokepoint, and rate-limit telemetry. The scalper runner's `_handle_exit` is extended to call both placements after an entry fills, record `tp_sl_armed` for each via `BinanceTestnetLedgerService`, and on either-side trigger cancel the opposite leg and transition the triggered side to `tp_sl_triggered`. Default `dry_run=True` propagates — the smoke CLI never places real orders unless an operator explicitly opts in per call.

**Tech Stack:** No new dependencies. Reuses ROB-286 (`testnet_execution.py`, signing chokepoint, ledger service, lifecycle state machine) and ROB-290 (split `BROKER_OPEN_STATES` / fills-side reconciliation). Tests reuse `pytest-httpx` for fake-client paths.

---

## What this PR contains (plan-only)

Exactly **one** file:

```
docs/plans/ROB-289-paired-tp-sl-broker-placement-implementation-plan.md
```

No app code. No tests. No migration. No scheduler config. No runtime activation. No real broker call. No live/testnet `--confirm` smoke. No secret values anywhere.

The implementation PR follows this plan PR (same `rob-289` branch, separate commits on top, separate merge); only at that PR's merge does ROB-289 close.

---

## 1. Scope (in the implementation PR — NOT in this plan PR)

### 1.1 New execution client methods

**File:** `app/services/brokers/binance/testnet/execution_client.py`

```python
async def place_stop_limit_order(
    self,
    *,
    symbol: str,
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    stop_price: Decimal,
    limit_price: Decimal,
    client_order_id: str,
    confirm: bool = False,
) -> StopOrderResult: ...

async def place_stop_market_order(
    self,
    *,
    symbol: str,
    side: Literal["BUY", "SELL"],
    quantity: Decimal,
    stop_price: Decimal,
    client_order_id: str,
    confirm: bool = False,
) -> StopOrderResult: ...
```

- Spot only. **No `reduceOnly`** parameter (would invite futures-path leakage; futures gated to ROB-291).
- Signed via existing `_sign_request_params` chokepoint. New HMAC implementations are forbidden.
- Routes through the existing `build_testnet_client` transport. No new host. No allowlist change.
- `confirm=False` default returns a `DryRunResult` with NO HTTP and no ledger mutation. `confirm=True` is required per call to place at the broker.
- Binance spot order types used:
  - TP leg: `STOP_LOSS_LIMIT` (acts as take-profit on the closing side of an open position) with `timeInForce=GTC`.
  - SL leg: `STOP_LOSS` (stop-market) with no `timeInForce`.
  - Lock these mappings explicitly in implementation — they are the testnet-supported variants for spot.
- All numeric fields serialize via existing precision helpers (no new precision logic).

### 1.2 New DTO

**File:** `app/services/brokers/binance/testnet/dto.py`

```python
@dataclass(frozen=True, slots=True)
class StopOrderResult:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["STOP_LOSS_LIMIT", "STOP_LOSS"]
    stop_price: Decimal
    limit_price: Decimal | None  # None for stop-market
    status: str  # broker-reported initial status (e.g., NEW)
    transact_time_ms: int
```

### 1.3 Runner wiring

**File:** `app/services/scalping/runner.py`

After an entry fill is detected in `tick_once` (or wherever the `record_fill` transition lands):

1. Generate two new `client_order_id`s: `<entry_cid>-tp` and `<entry_cid>-sl` (deterministic suffix; helps reconciliation tie them back).
2. Compute TP/SL prices from `ScalperConfig.tp_pct` / `sl_pct` against the entry fill price.
3. Call `place_stop_limit_order` (TP leg) and `place_stop_market_order` (SL leg) — **sequential, not parallel** (parallel placement can produce ambiguous half-armed state on a broker reject; sequential lets the failure path be deterministic).
4. After each placement returns successfully, call `ledger_service.record_tp_sl_armed(client_order_id=..., parent_client_order_id=<entry_cid>, broker_order_id=<from result>, tp_or_sl="tp"|"sl", stop_price=..., limit_price=...)`.
5. Existing `_derive_symbol_state` continues to read these rows for "symbol busy" detection.

### 1.4 Opposite-leg cancellation on trigger

When the runner's reconciliation (or its tick loop, if a future iteration adds live fill polling) detects that one of the two `tp_sl_armed` rows transitioned to filled at the broker:

1. Call `record_tp_sl_triggered(client_order_id=<triggered_cid>)` on the triggered leg.
2. Look up the sibling row by `parent_client_order_id` (the shared entry CID).
3. Call `cancel_order(client_order_id=<sibling_cid>, confirm=True)` at the broker.
4. Call `record_cancel(client_order_id=<sibling_cid>, reason="opposite_leg_triggered")` on the sibling.
5. Call `record_close(client_order_id=<entry_cid>)` on the entry row (or whichever step the state machine requires to close the parent — locked by Task 9 of ROB-286 plan).
6. Existing `record_reconcile` completes the lifecycle.

### 1.5 Broker-reject fallback

If `place_stop_limit_order` OR `place_stop_market_order` returns a 4xx (not a timeout / 5xx):

1. Do **not** silently swallow.
2. If the FIRST leg placed succeeded and the SECOND leg failed, immediately call `cancel_order` on the first leg to avoid leaving a half-armed broker position.
3. Record `record_anomaly(client_order_id=<entry_cid>, reason="tp_sl_placement_rejected", extra_metadata={...broker rejection payload (no secrets)...})` on the entry row.
4. Fall back to the existing synthetic cancel-and-close path: place a plain market sell (or whatever the current `_handle_exit` does) and complete the lifecycle with `tp_sl_triggered` skipped and `close` reason annotated as `fallback_after_broker_reject`.
5. **No retry** on the rejected leg in the same tick — a future PR can add retry/backoff if needed; this PR keeps the behavior deterministic.

### 1.6 Documentation

- Update `docs/runbooks/binance-testnet-scalping.md` §7 ("cancel-and-close fallback") — switch the default-path description from "cancel-and-close" to "paired TP/SL stop orders", and document the fallback as the explicit reject-handler.
- Update CLAUDE.md ledger section if any lifecycle transitions need clarification (they shouldn't — ROB-286 already locked the state machine).

---

## 2. Safety boundaries (every test in §4 maps to one of these)

1. **Binance testnet only.** All new code lives under `app/services/brokers/binance/testnet/`. No live Binance host (`api.binance.com`, `fapi.binance.com`) reachable from any new path. Audit `test_no_live_host_url_in_testnet_package` (T33 from ROB-286) continues to enforce.
2. **Spot only.** No futures SDK added (`binance-sdk-derivatives-trading-usds-futures` stays absent from `pyproject.toml`). No `testnet.binancefuture.com` in `TESTNET_HOSTS`.
3. **No `reduceOnly` parameter** in any signature. Spot doesn't use it; adding it now would create a footgun for a future-path PR. Audit grep on `--include="*.py"` confirms zero hits in `app/services/brokers/binance/testnet/`.
4. **No new live Binance host.** `TESTNET_HOSTS` unchanged. Cross-allowlist disjointness (`PUBLIC_HOSTS ∩ TESTNET_HOSTS = ∅`) preserved.
5. **No production deploy.** This PR's merge does NOT trigger any deploy step.
6. **No production DB `alembic upgrade head`.** No new migration in this PR. Implementation PR adds no migration either — the ledger schema from ROB-286 supports all needed transitions already.
7. **No production scheduler / TaskIQ / cron activation.** `test_no_scheduler_activation` (T34) continues to enforce; no entries added to `app/core/scheduler.py`, `app/core/taskiq_broker.py`, `app/tasks/`. Implementation PR audit re-runs.
8. **No real-money mutation.** Class remains `BinanceTestnetExecutionClient`; no "live" mode exists structurally.
9. **No KR/US/Upbit/Alpaca/KIS/Kiwoom changes.** Sibling broker packages untouched.
10. **No live/testnet `--confirm` smoke in this PR or the implementation PR.** Live testnet smoke is ROB-293 (operator-gated).
11. **No secret printed.** `test_secret_not_in_logs_on_init_failure` invariant from ROB-286 continues to enforce. Implementation PR adds a new test that exercises the failure path of `place_stop_limit_order` / `place_stop_market_order` with `caplog.at_level(DEBUG)` and asserts the API secret string never appears in captured log records.
12. **Default `dry_run=True`** propagates through every new method. `confirm=True` is required per call to place at the broker. The smoke CLI default-disabled invariant (T31) continues to hold; dry-run smoke (T32) continues to produce zero `submitted` rows (and therefore zero `tp_sl_armed` rows).
13. **No unsafe-config silent continue.** Missing credentials → `BinanceMissingCredentials` at adapter construction (existing T11/T12 invariants).

---

## 3. Failure / fallback behavior (locked, not optional)

### 3.1 First leg placed, second leg rejected

- Sequential placement (§1.3) means the runner observes the first success before attempting the second.
- On second-leg rejection, the runner MUST immediately `cancel_order(confirm=True)` on the first leg.
- This is the most dangerous failure path — half-armed broker state is worse than no-armed state.

### 3.2 First leg rejected

- Skip second leg entirely.
- Record `record_anomaly` on the entry row with `reason="tp_sl_placement_rejected"` and the broker error code in `extra_metadata` (NEVER include API key/secret or the raw signed query string).
- Fall back to the cancel-and-close path (existing behavior pre-ROB-289).

### 3.3 Both legs placed; trigger detected; sibling cancel fails

- Record `record_anomaly` on the sibling row with `reason="opposite_leg_cancel_failed"`.
- Operator action required; do not auto-retry.
- Existing reconciliation (`reconcile_on_start` from ROB-286 + ROB-290) will pick up the dangling broker order on next runner startup and either close it via cancel or escalate.

### 3.4 Missing credentials / disabled config

- Standard fail-closed at construction. No new failure mode introduced by ROB-289.

### 3.5 Partial network failure mid-placement (timeout, 5xx)

- Treat as "unknown state" — do NOT assume the order didn't place.
- Record `record_anomaly` on the in-flight leg with `reason="tp_sl_placement_unknown"`.
- Reconciliation on next runner startup walks `open_orders` + `recent_fills` and resolves the row deterministically.

### 3.6 Ledger auditability invariant

Every broker action (place, cancel, trigger detection) has a corresponding ledger transition. There is no "silent" broker write. If the implementation finds a need for one, stop and escalate.

---

## 4. Tests to be added in the implementation PR

This plan does NOT add tests (plan-only). The list below is the contract the implementation PR satisfies.

| # | Test | File | What it proves |
|---|---|---|---|
| TT1 | `test_place_stop_limit_order_dry_run_no_http` | `test_execution_client_submit_cancel_fake.py` | `confirm=False` (default) returns `DryRunResult`, zero HTTP |
| TT2 | `test_place_stop_limit_order_confirmed_hits_testnet_host` | same | `confirm=True` POST to `testnet.binance.vision/api/v3/order` with `type=STOP_LOSS_LIMIT` |
| TT3 | `test_place_stop_market_order_dry_run_no_http` | same | parallel to TT1 |
| TT4 | `test_place_stop_market_order_confirmed_hits_testnet_host` | same | parallel to TT2; `type=STOP_LOSS` |
| TT5 | `test_place_stop_orders_reject_non_testnet_host` | `test_transport_event_hooks.py` (extension) | Cross-allowlist guard intact for the new methods |
| TT6 | `test_place_stop_orders_no_reduceonly_param` | `test_audit_no_live_host.py` (extension) | Source audit — no `reduceOnly` literal in `binance/testnet/` (the previous broader audit already covered `app/`; extend the precise location check) |
| TT7 | `test_runner_places_tp_sl_pair_after_entry_fill` | `test_runner_lifecycle.py` (extension) | Full path: entry submitted → filled → both TP and SL `tp_sl_armed` recorded with `parent_client_order_id` linkage |
| TT8 | `test_runner_cancels_opposite_leg_when_tp_triggers` | `test_runner_lifecycle.py` (extension) | TP triggers → SL cancelled via broker cancel + ledger `record_cancel` |
| TT9 | `test_runner_cancels_opposite_leg_when_sl_triggers` | same | Mirror of TT8 |
| TT10 | `test_first_leg_success_second_leg_reject_cancels_first` | `test_runner_lifecycle.py` (extension) | §3.1 — most dangerous path; first-leg-success-then-second-leg-fail must immediately cancel the first leg |
| TT11 | `test_first_leg_reject_falls_back_to_cancel_and_close` | same | §3.2 fallback path |
| TT12 | `test_sibling_cancel_failure_records_anomaly` | same | §3.3 — operator-investigatable anomaly |
| TT13 | `test_placement_network_timeout_records_unknown_state` | same | §3.5 — unknown state ledger transition |
| TT14 | `test_secret_not_in_logs_during_stop_placement_failure` | `test_execution_client_fail_closed.py` (extension) | §11 of safety boundaries — API secret never appears in any captured log line during a simulated placement failure |
| TT15 | `test_smoke_dryrun_still_creates_no_submitted_rows` | `test_binance_testnet_scalper_smoke.py` (existing — re-run, no change) | T32 from ROB-286 + ROB-290 patch invariant holds; dry-run smoke still issues zero `/api/v3/order` POSTs |
| TT16 | `test_existing_T1_through_T35_continue_to_pass` | (no new file) | Full ROB-286 matrix re-run as regression |
| TT17 | `test_rob_290_fills_reconciliation_continues_to_pass` | (no new file) | ROB-290's 3 new reconciliation tests re-run as regression |

The implementation PR's self-review must list each row as pass / skipped + reason; none may be silently omitted.

---

## 5. Non-goals (explicit deferrals)

| Item | Owner | Reason for deferral |
|---|---|---|
| Futures `reduceOnly` exit enforcement | **ROB-291** | Spot has no `reduceOnly`; futures path deferred to a separate child plan with its own SDK + runbook |
| Production scheduler / TaskIQ / cron activation | **ROB-292** | Operator gate; pre-conditions include ROB-289 + ROB-290 + ROB-293 success |
| Operator-confirmed live testnet `--confirm` smoke | **ROB-293** | Requires real testnet credentials; operator-initiated only |
| Production DB cutover (`alembic upgrade head` on prod) | Operator runbook | Migration-by-migration cutover, separate from any merge |
| Retry/backoff on broker reject | Future iteration | This PR keeps reject behavior deterministic; retry can be added later if reject rate justifies it |
| Pre-trade margin / balance check before placing TP/SL | Future iteration | Testnet's balance is operator-funded; spot stop orders don't lock new margin |
| Discord / Hermes notification on anomaly | Future iteration | Sentry breadcrumb on `anomaly` is the MVP signal; richer notification deferred |

---

## 6. PR / implementation sequencing

### This PR (plan-only)

- Exactly one changed file: `docs/plans/ROB-289-paired-tp-sl-broker-placement-implementation-plan.md`.
- PR title: `docs(rob-289): add paired TP/SL broker placement implementation plan`.
- PR body uses `Refs ROB-289` (do NOT close the issue yet).
- After review/approval and merge, the implementation PR starts on the same `rob-289` branch (the branch is recreated from latest `origin/main` after the plan squash, same flow as Children A/B/C).

### Implementation PR (follows; NOT this PR)

- Single PR with TDD commits per section §1.1 → §1.6 + tests TT1 → TT17.
- Final self-review against:
  - All 17 tests in §4 pass or have explicit server-only blocker reasons.
  - 13 safety boundaries from §2 each map to ≥1 passing test.
  - Lint trio green (`ruff check`, `ruff format --check`, `ty check`).
  - Audit greps (no live host literal in `binance/testnet/`, no `reduceOnly` in spot signatures, no scheduler entries, no sibling broker mutation, no public adapter changes).
  - ROB-286 + ROB-290 regression sets remain green.
  - Smoke dry-run + smoke default-disabled invariants hold.
- After implementation merge AND post-merge main CI green, ROB-289 closes.

### NOT in either PR

- No `alembic upgrade head` against production DB.
- No scheduler activation.
- No deploy.
- No operator-confirmed `--confirm` smoke (separate Linear issue ROB-293).
- No live order to live Binance.

---

## 7. Reviewer focus points for the implementation PR (when it arrives)

1. **§3.1 (first leg succeeded, second rejected)** — most dangerous failure path. Verify the cancel of the first leg is awaited synchronously before returning from the placement function; verify it cannot be skipped on exception paths.
2. **Cross-allowlist guard intact** — the new methods MUST route through `build_testnet_client`'s event hooks. Verify TT5 actually exercises a non-testnet host injection and raises.
3. **Sequential vs parallel placement** — confirm placement is sequential (`await TP; await SL`), not `asyncio.gather`. Parallelism here trades determinism for ~50ms of latency — not worth it.
4. **`parent_client_order_id` linkage correctness** — sibling lookup in §1.4 must be by the SHARED entry CID; never by the TP/SL CIDs themselves (those would be self-references).
5. **HMAC chokepoint** — the new methods MUST call `_sign_request_params`; no inline HMAC. Grep `app/services/brokers/binance/testnet/` for any non-chokepoint signing.
6. **Audit `reduceOnly` absence** — TT6 grep must explicitly fail if `reduceOnly` appears anywhere in `binance/testnet/`. Spot-side this parameter is meaningless; introducing it (even unused) is a future-leak risk.
7. **Secret-in-logs (TT14)** — extend the existing ROB-286 test pattern to the new placement-failure paths. Specifically, on a 4xx broker reject, ensure the captured log message includes the broker error code but NOT the signed query string (which contains `signature=`, an HMAC of the secret — even leaking the HMAC is a concern).
8. **Smoke dry-run invariant (TT15)** — ROB-290's patch already covered fills-side; ROB-289 must not regress dry-run smoke by issuing `/api/v3/order` POSTs unexpectedly. The fact that no `submitted` rows are created in dry-run smoke means no `filled` transition either, which means no TP/SL placement is reached — but verify explicitly.
9. **Ledger lifecycle** — verify the runner records BOTH `record_tp_sl_armed` calls successfully before the function returns; partial recording (one row armed, other not, no anomaly) is the most insidious bug.
10. **Idempotency on restart** — if the runner crashes after placing TP but before recording `tp_sl_armed`, the next startup's reconciliation must catch the orphan broker order and reconcile (either retroactively record `tp_sl_armed` or cancel the broker order + record anomaly). Verify TT13 or an extension covers this.

---

## 8. Operator boundary (unchanged from ROB-286)

- ❌ No production deploy by either PR's merge.
- ❌ No production DB `alembic upgrade head` (no migration in either PR anyway).
- ❌ No production scheduler / TaskIQ / cron activation (ROB-292 gate).
- ❌ No operator-confirmed live testnet `--confirm` smoke in either PR (ROB-293 gate).
- ❌ No Binance live order, Alpaca live order, KIS / Upbit live order.
- ❌ No real-money mutation.
- ❌ No futures SDK expansion (ROB-291 gate).
- ❌ No secret values printed.

---

## 9. Self-review (plan PR)

- [x] Exactly one file changed (`docs/plans/ROB-289-paired-tp-sl-broker-placement-implementation-plan.md`).
- [x] No app code, no tests, no migration, no scheduler config, no runtime activation.
- [x] No real broker call.
- [x] No live/testnet `--confirm` smoke.
- [x] No secret values anywhere in the plan text.
- [x] Scope (§1) covers: new client methods, runner wiring, opposite-leg cancel, broker-reject fallback, runbook update.
- [x] Safety boundaries (§2) enumerated; 13 items, each maps to ≥1 test in §4.
- [x] Failure/fallback (§3) covers: first-leg-success-second-reject (§3.1), first-leg-reject (§3.2), sibling-cancel-fail (§3.3), missing-creds (§3.4), network-timeout (§3.5), ledger-auditability (§3.6).
- [x] Test list (§4) has 17 rows, each with file location + what-it-proves.
- [x] Non-goals (§5) enumerate 7 deferrals with owners + reasons.
- [x] PR sequencing (§6) explicit: this PR plan-only, impl PR follows, ROB-289 closes only after impl PR merge + main CI green.
- [x] Reviewer focus points (§7) surface 10 spot-check areas for the impl PR.
- [x] Operator boundary (§8) re-confirms the standing list.
- [x] No placeholders / TBD / TODO.
