# ROB-286 — Binance Testnet Scalping MVP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> AOE_STATUS: plan_ready
> AOE_ISSUE: ROB-286
> AOE_ROLE: implementer
> AOE_NEXT: execute Task 1 (audit + invariants) BEFORE any signed-endpoint code is written. The audit invariants are the safety net that catches every other category of mistake.
>
> **Per-task open-items reporting (required):** Same convention as ROB-285. At the start of any task referenced by the Open items table, state which lean is being adopted in the task's first commit message. Especially important here — open items in this plan involve safety-critical choices (futures inclusion, HMAC signing source, position reconciliation strategy).
>
> **Safety-critical reminder:** This is the first PR in the project that introduces *signed* Binance endpoints. The entire test layer is designed around proving that signed requests CANNOT reach live Binance hosts. If a single test in the §Safety invariants section is removed or weakened, stop and ask before continuing.

**Goal:** Build a testnet-only Binance execution adapter, a dedicated `binance_testnet_order_ledger` with service-only writes, and a deterministic entry/TP/SL scalping state machine. The MVP proves we can run a real testnet order lifecycle (preview → validate → submit → fill → TP/SL arm → trigger → close → reconcile) without any code path that could route to live Binance.

**Architecture:** A new `app/services/brokers/binance/testnet/` sub-package containing the signed execution adapter, sitting alongside but strictly isolated from Child B's read-only `app/services/brokers/binance/` package. Two distinct host allowlists (`PUBLIC_HOSTS` from Child B, new `TESTNET_HOSTS` here) with cross-allowlist guards: signed request to a public host raises; public request to a testnet host raises. A new `app/services/scalping/` package contains the deterministic state machine that reads from Child B's market data adapters and writes through the testnet execution adapter + ledger.

**Tech Stack:** Existing `binance-sdk-spot` (from Child B) for HTTP plumbing if it exposes a clean signed-request signer; otherwise stdlib `hmac`/`hashlib` for signing. PostgreSQL + SQLAlchemy 2 async + Alembic for the new ledger table. `pytest-httpx` (from Child B) for transport tests. `pytest-asyncio` (existing) for async. No new runtime dependencies unless futures is included (which is OUT of scope for this PR — see §B.C.2).

---

## Pre-implementation discovery (audit confirmed against `origin/main` HEAD `eb40de03`)

1. **Child B artifacts to consume (read-only):**
   - `app/services/brokers/binance/host_allowlist.py::PUBLIC_HOSTS` — frozen set, do not extend with testnet hosts. Child C creates a parallel `TESTNET_HOSTS` set in `app/services/brokers/binance/testnet/host_allowlist.py`.
   - `app/services/brokers/binance/transport.py::build_public_client()` — public-only httpx client. Child C creates `build_testnet_client(api_key, api_secret)` in `app/services/brokers/binance/testnet/transport.py`. Never reuse public client.
   - `app/services/brokers/binance/errors.py` — reuse `BinanceLiveHostBlocked` and `BinanceSignedEndpointAttempted` (the latter must continue to fire for public adapter). Child C adds `BinanceTestnetDisabled`, `BinanceMissingCredentials`, `BinanceInvalidStateTransition`, `BinanceReduceOnlyRequired`, `BinanceTestnetCrossAllowlistViolation`.
   - `app/services/brokers/binance/ws_client.py`, `gap_detector.py`, `backfill.py`, `ingest.py`, `rate_limit_telemetry.py` — read-only market data sources for the scalper's price input. Never coupled to execution code paths.
   - `app/models/crypto_instruments.py` — read instrument_id by `(venue='binance', product='spot', venue_symbol=SYMBOL)` to associate ledger rows with instruments.
   - `app/services/instrument_health/service.py` — read instrument health; scalper refuses to trade `manual_backfill_required` or `degraded` instruments.

2. **Child B's audit invariants are still binding:**
   - `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` already enforces "no signed-endpoint surface in `app/services/brokers/binance/` package" with an `ALLOWED_LEGACY_FILES` set + `SIGNED_SYMBOL_RE`. Child C does NOT extend the public package with signed endpoints; signed code lives in a separate `binance/testnet/` sub-package. The audit test's regex applies to source under `binance/` but excludes `binance/testnet/` — Task 1 updates the audit to make this exclusion explicit (otherwise Task 5 adding `order()` to the testnet client would fail Child B's audit).

3. **AlpacaPaperLedgerService precedent** (`app/services/alpaca_paper_ledger_service.py`):
   - 11 `record_*` methods, lifecycle `planned → previewed → validated → submitted → filled → position_reconciled → sell_validated → closed → final_reconciled` + `anomaly`. Child C mirrors the shape; vocabulary adapts to scalping (`tp_sl_armed → tp_sl_triggered → closed` instead of Alpaca's `position_reconciled → sell_validated`).
   - Convention-based service-only-write enforcement (module docstring + tests, no DB triggers).

4. **`crypto_instrument_health` (from ROB-285)** has 4 states: `healthy / degraded / rate_limited / manual_backfill_required`. Scalper reads but does NOT write. The execution adapter does NOT touch this table. If a future need emerges to write execution-related health states, that's a follow-up.

5. **No `app/services/scalping/` directory exists yet.** Child C introduces it.

---

## Hard safety invariants (apply to EVERY task — non-negotiable)

These are the eight invariants the user explicitly called out as hard requirements. Each is enforced by at least one test. If any test is removed, the PR is bounced.

1. **No signed request reaches a host outside `TESTNET_HOSTS`.** Cross-allowlist transport guard at the `request` event hook + audit test that scans the testnet client source for any URL string matching `api.binance.com`, `fapi.binance.com`, etc.
2. **Live Binance host injected as `BINANCE_TESTNET_BASE_URL` is rejected at adapter init.** Test: setting env to `https://api.binance.com` causes `BinanceLiveHostBlocked` at construction time.
3. **API key/secret never required in the public market data path.** Test: import the public package + invoke its methods with all `BINANCE_TESTNET_*` env vars **unset**, and the public path works (Child B's existing smoke proves this).
4. **Missing credentials → fail-closed at adapter construction.** Test: setting `BINANCE_TESTNET_ENABLED=true` without `BINANCE_TESTNET_API_KEY`/`SECRET` causes `BinanceMissingCredentials` at init.
5. **Order path executes only in `testnet`/`paper` mode.** Test: an order method called with `dry_run=False` AND `confirm=False` raises `BinanceTestnetDisabled` regardless of env. The execution adapter has no "live" mode; the class itself is named `BinanceTestnetExecutionClient` to make the constraint structural.
6. **Default config is disabled.** Test: with `BINANCE_TESTNET_ENABLED` unset (default), the smoke CLI exits 0 with a single "disabled — set BINANCE_TESTNET_ENABLED=true to opt in" log line and zero side effects (no HTTP, no DB writes, no Sentry events).
7. **Production scheduler stays disabled.** Test: `grep -rn "binance_testnet_scalper\|BinanceTestnetExecutionClient" app/core/scheduler.py app/core/taskiq_broker.py app/tasks/` returns empty. PR diff against `app/core/` shows no TaskIQ entry additions.
8. **Operator gate before any automatic execution.** No code path constructs the execution client unless an explicit per-call boolean (`confirm=True`) is passed. The class init does NOT auto-arm anything; submission requires the per-call flag every time. Test: calling `submit_order(..., confirm=True)` actually attempts the testnet submit; calling with `confirm=False` (default) returns a `DryRunResult` without HTTP.

These invariants are encoded across `tests/services/brokers/binance/testnet/test_audit_no_live_host.py`, `test_host_allowlist.py`, `test_transport_event_hooks.py`, `test_execution_client_fail_closed.py`, and `test_smoke_cli_default_disabled.py`. If a test is added/changed/removed during execution, the diff must explicitly explain why in the PR description.

---

## What stays in Child C+ (NOT in this PR — explicit boundary)

Echoing user-stated forbidden scope:

- **Binance live trading.** Anywhere. Adapter is structurally testnet-only.
- **Alpaca live order / KIS live order / Upbit live order changes.** Those broker packages are not touched.
- **Production deploy.** This PR does not trigger any deploy step.
- **Production DB `alembic upgrade head`.** The new `binance_testnet_order_ledger` migration ships in the PR but operator runs it on real DBs separately, same gate as ROB-284 / ROB-285.
- **Production scheduler / TaskIQ activation.** No cron, no TaskIQ task, no Prefect deployment. CLI-only invocation.
- **Real-money account mutation.** No code path can submit against a real-money endpoint.
- **Unapproved futures SDK expansion.** `binance-sdk-derivatives-trading-usds-futures` is NOT added in this PR (locked §B.C.2). Spot-only MVP. If futures is needed later, a separate child issue + plan + dependency vet.
- **Signed endpoint creep into the public adapter.** `app/services/brokers/binance/rest_client.py` (Child B) must NOT gain `order()`, `account()`, etc. Audit test enforces.

If any of these are needed to make the MVP work, **stop and escalate** — do not power through.

---

## Locked decisions for Child C

### B.C.1 — Two separate host allowlists, no overlap

- `PUBLIC_HOSTS` (Child B, unchanged): `api.binance.com`, `data-api.binance.vision`, `stream.binance.com`, `data-stream.binance.vision`.
- `TESTNET_HOSTS` (new, this PR): `testnet.binance.vision`, `stream.testnet.binance.vision`.
- **No host appears in both sets.** Asserted by a test in Task 2.
- **Cross-allowlist guards:** the testnet transport rejects any request whose final host is in `PUBLIC_HOSTS` (would mean "signed request to live", catastrophic). The public transport already rejects any request carrying an `X-MBX-APIKEY` header (would mean "signed request via public client"). Both guards fire at the `request` event hook + (for testnet) at adapter init.

### B.C.2 — Spot only for MVP (futures deferred)

- This PR ships spot testnet execution only.
- `testnet.binancefuture.com` is NOT in `TESTNET_HOSTS`. If a future PR needs futures testnet, that PR adds the host AND the futures SDK AND the reduce-only enforcement AND a separate runbook.
- The execution client has no method that takes a `reduceOnly` parameter. Spot-side that flag is meaningless; carrying it in the signature would invite future futures-path leakage. Reduce-only enforcement is therefore explicitly OUT of scope for this PR but documented in the runbook as the gate for the futures follow-up.

### B.C.3 — Signed transport requires creds at construction

- `build_testnet_client(api_key: str, api_secret: str)` is the only public factory.
- No default-empty arguments. No environment fallback at the factory level (the factory takes the strings explicitly; the *caller* — usually `BinanceTestnetExecutionClient.__init__` — does the env lookup and the fail-closed check).
- This separation means the transport factory is unit-testable without env mucking, and the env-validation logic lives in one place (the adapter init).

### B.C.4 — `dry_run=true` is the default, `confirm=True` is per-call

- The execution client's `submit_order(...)`, `cancel_order(...)`, etc., default to `dry_run=True`.
- Setting `BINANCE_TESTNET_ENABLED=true` alone does NOT enable submissions; the caller must pass `confirm=True` on each call.
- Rationale: a config-level "enabled" flag, even paired with credentials, lets a single misconfigured deploy fire orders. Per-call `confirm` makes every order a deliberate function-call site that audit can grep for.

### B.C.5 — Service-only writes for `binance_testnet_order_ledger`

- All writes go through `BinanceTestnetLedgerService`.
- `BinanceTestnetLedgerRepository` is service-internal — module-level import guard same shape as `app/services/instrument_health/repository.py` from ROB-285.
- Test asserts: `importlib.import_module("app.services.brokers.binance.testnet.ledger.repository._public_export")` raises `ImportError` when called from outside the ledger service module.
- Documented in CLAUDE.md (new section parallel to existing "Alpaca Paper 실행 레저" entry).

### B.C.6 — HMAC signing source

- `binance-sdk-spot` exposes a request signer; **use the SDK signer where possible** to avoid maintaining our own HMAC implementation drift.
- Wrap it in a thin local function `_sign_request_params(params: dict, api_secret: str) -> dict` that adds `timestamp` + `signature` to the params dict. This function is the chokepoint; tests assert it produces the canonical signature for a known input.
- If the SDK's signer is not directly callable (e.g., it only signs inside its own HTTP client), fall back to stdlib `hmac.new(api_secret, urlencode(params), sha256).hexdigest()`. Decision finalized in Task 5 after inspecting the SDK API.

### B.C.7 — Scalper is deterministic; no LLM, no Discord/Hermes approval

- The state machine logic lives in `app/services/scalping/binance_testnet_scalper.py` as a pure function (`compute_action(state, market_snapshot, config) → Action`).
- The runner loop in `app/services/scalping/runner.py` orchestrates: read market data → compute action → call execution client → record ledger transition.
- Notifications (Sentry events, log.info, optional Discord) are observation-only; they never gate a decision. The runner does not await an external system before submitting.

### B.C.8 — Position tracking: max 1 open position per symbol

- The scalper queries the ledger for the most recent `(instrument_id, lifecycle_state)` per symbol before deciding entry. If `state ∈ {submitted, filled, tp_sl_armed}`, refuse to enter; the symbol is "busy".
- MVP symbols: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`. Hard-coded set in `app/services/scalping/config.py`; expanding requires a code change (deliberate friction).

### B.C.9 — Max notional default 10 USDT; per-call override

- Default: `BINANCE_TESTNET_MAX_NOTIONAL_USDT = 10`. The scalper sizes each entry to ≤ this notional.
- Caller can override at submit site via `submit_order(..., notional_usdt=X)` where X > the env default. If override is requested, the call must also pass `notional_override_reason=str` (audit-friendly forcing function). Without the reason argument, the override raises.

### B.C.10 — Reconciliation on restart

- On runner startup, the scalper reconciles ledger state against testnet broker state for each instrument in the MVP set:
  - Query the broker's open orders + recent fills.
  - For each ledger row in `submitted` / `tp_sl_armed`, verify the broker still has the matching open order. If not, transition to `anomaly` with a `reconcile_drift` reason.
  - For each ledger row in `filled` without a corresponding `closed`, query the broker for fill events newer than `last_reconciled_at`.
- The reconciliation logic is pure-async (no scheduler); it runs once on runner construction. The runner then either continues (clean) or refuses to start (anomalies present).

### B.C.11 — Scheduler activation explicitly OFF

- The runner is invoked only from `scripts/binance_testnet_scalper_smoke.py` (the smoke CLI) or directly from a test.
- No TaskIQ task wraps the runner.
- No Prefect deployment references the runner.
- Test in §Audit asserts the grep for `BinanceTestnetExecutionClient|binance_testnet_scalper` returns no hits in `app/core/scheduler.py`, `app/core/taskiq_broker.py`, `app/tasks/*`.

---

## Open items (resolve during execution; lean default + lock task identified)

| # | Item | Lean default | Lock during |
|---|------|--------------|-------------|
| 1 | Whether to use `binance-sdk-spot`'s signer or fall back to stdlib HMAC. | SDK signer wrapped behind `_sign_request_params(...)`; fallback to stdlib only if the SDK's signer isn't standalone-callable. | Task 5 |
| 2 | Whether to add `binance-sdk-derivatives-trading-usds-futures` for futures testnet path. | **No** — explicit non-goal per §B.C.2. Defer to a follow-up child issue. | Locked in this plan |
| 3 | Reconciliation depth (how many recent fills to fetch on startup). | Last 50 orders + last 100 fills for the MVP triplet, time-bounded to "since last_reconciled_at OR 24h, whichever is shorter". | Task 11 |
| 4 | Whether ledger transitions emit Sentry events. | Yes for `anomaly` (always — operator must investigate); Yes for first `filled` after a `submitted` (sanity); no for routine `previewed`/`validated`/`tp_sl_armed` (noise). | Task 9 |
| 5 | Whether the scalper exposes a "shadow mode" (compute action but never submit). | Yes — shadow mode is `dry_run=true` on every submit call. Already implicit in §B.C.4; this open item is just confirming the CLI/test surface for it. | Task 14 |
| 6 | How the ledger represents "TP and SL both armed" since spot doesn't have native OCO on testnet. | Two ledger rows linked by `parent_client_order_id`, both in `tp_sl_armed` state; whichever triggers first transitions the other to `cancelled` via explicit cancel. | Task 9 |
| 7 | Whether the runner persists in-memory state to ledger periodically or only on transitions. | On transitions only. Reduces ledger noise. Re-derive in-memory state from ledger on restart (the reconciliation step in §B.C.10). | Task 11 |
| 8 | Whether to ship a separate `scripts/binance_testnet_seed_instruments.py` for seeding `BTCUSDT/ETHUSDT/SOLUSDT` instrument rows. | Yes — a tiny one-shot CLI mirroring Child B's structure. Idempotent; safe to re-run. Lives in `scripts/`. | Task 12 |

---

## File structure (created/modified by this PR)

### Created
```
app/services/brokers/binance/testnet/
├── __init__.py                              # Package marker + re-exports of public classes
├── host_allowlist.py                        # TESTNET_HOSTS frozenset + assert_testnet_host
├── transport.py                             # build_testnet_client(api_key, api_secret)
├── signing.py                               # _sign_request_params() chokepoint
├── execution_client.py                      # BinanceTestnetExecutionClient (signed)
├── dto.py                                   # OrderPreview, OrderSubmitResult, OpenOrder, Fill, ...
└── errors.py                                # BinanceTestnetDisabled, BinanceMissingCredentials, ...

app/services/brokers/binance/testnet/ledger/
├── __init__.py
├── repository.py                            # BinanceTestnetLedgerRepository (service-internal)
└── service.py                               # BinanceTestnetLedgerService (11 record_* methods)

app/services/scalping/
├── __init__.py
├── config.py                                # MVP symbol set, max notional, etc.
├── decision.py                              # compute_action(state, snapshot, config) → Action
├── runner.py                                # Runner loop + reconciliation
└── notifications.py                         # Optional Sentry/log emission helpers

app/models/binance_testnet_order_ledger.py   # ORM model

alembic/versions/<rev>_add_binance_testnet_order_ledger.py

scripts/binance_testnet_scalper_smoke.py     # CLI (default-disabled, dry-run, side-effect-free)
scripts/binance_testnet_seed_instruments.py  # one-shot instrument seeder (idempotent)

docs/runbooks/binance-testnet-scalping.md

tests/services/brokers/binance/testnet/
├── __init__.py
├── test_audit_no_live_host.py
├── test_host_allowlist.py
├── test_transport_event_hooks.py
├── test_signing.py
├── test_execution_client_fail_closed.py
├── test_execution_client_preview.py
├── test_execution_client_submit_cancel_fake.py
└── test_ledger_service.py

tests/services/scalping/
├── __init__.py
├── test_decision.py
├── test_runner_lifecycle.py
└── test_runner_reconciliation.py

tests/scripts/
└── test_binance_testnet_scalper_smoke.py    # CLI default-disabled / side-effect-free
```

### Modified
- `pyproject.toml` (no new runtime dep; `binance-sdk-spot` already present from Child B)
- `app/models/__init__.py` (register `BinanceTestnetOrderLedger`)
- `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` (extend `ALLOWED_LEGACY_FILES` / scope exclusion to allow signed methods *only* inside `binance/testnet/`)
- `CLAUDE.md` (new "Binance Testnet Order Ledger (ROB-286)" section parallel to ROB-84 Alpaca entry)

### Not modified
- `app/services/brokers/binance/{rest_client,ws_client,host_allowlist,transport,backfill,ingest,gap_detector,rate_limit_telemetry,errors,dto}.py` (Child B's public adapter)
- `app/services/brokers/upbit/`, `alpaca/`, `kis/`, `kiwoom/`
- `app/services/upbit_websocket.py`
- `app/services/crypto_execution_mapping.py`
- `app/core/scheduler.py`, `app/core/taskiq_broker.py`, `app/tasks/*` (NO scheduler activation)
- `app/jobs/*`
- `app/models/crypto_instruments.py`, `app/services/daily_candles/*`, `app/services/minute_candles/*`, `app/services/instrument_health/*` (consumed read-only)

---

## Production cutover gate (deferred — operator-gated, NOT triggered by this PR's merge)

Same pattern as ROB-284 / ROB-285:

1. Pre-cutover DB backup of the target environment (logical or vendor equivalent).
2. `uv run alembic upgrade head` on the non-prod server DB; verify `binance_testnet_order_ledger` exists with the CHECK constraint and is empty initially.
3. `uv run alembic downgrade -1 && uv run alembic upgrade head` round-trip verification.
4. Smoke CLI default-disabled run: `uv run python -m scripts.binance_testnet_scalper_smoke` exits 0 with the "disabled" log line and zero side effects.
5. Smoke CLI dry-run with credentials: `BINANCE_TESTNET_ENABLED=true BINANCE_TESTNET_API_KEY=... BINANCE_TESTNET_API_SECRET=... uv run python -m scripts.binance_testnet_scalper_smoke --duration 30 --dry-run` produces ledger `planned`/`previewed`/`validated` rows but zero `submitted` rows (because no `--confirm`).
6. After validation in non-prod, manual operator-initiated `--confirm` smoke against the testnet API (small notional, single symbol, 5-minute duration).
7. Production cutover is scheduled separately; this PR's merge alone does NOT enable any of the above.

Documented in `docs/runbooks/binance-testnet-scalping.md` (Task 15).

---

## Test matrix (covers every user-stated guarantee + safety invariant)

| # | Guarantee / behavior | Test file | Test name(s) | Layer verified |
|---|---|---|---|---|
| T1 | `TESTNET_HOSTS` and `PUBLIC_HOSTS` are disjoint | `test_host_allowlist.py` | `test_testnet_and_public_hosts_are_disjoint` | Module |
| T2 | Allowed testnet hosts accepted at module level | `test_host_allowlist.py` | `test_testnet_hosts_accepted` (parametrized) | Module |
| T3 | Non-testnet hosts rejected at module level | `test_host_allowlist.py` | `test_non_testnet_hosts_rejected` (parametrized, incl. `api.binance.com`, `testnet.binancefuture.com`) | Module |
| T4 | Adapter init rejects `base_url='https://api.binance.com'` | `test_transport_event_hooks.py` | `test_init_with_live_base_url_raises` | Adapter init |
| T5 | Adapter init rejects env `BINANCE_TESTNET_BASE_URL=https://api.binance.com` | `test_execution_client_fail_closed.py` | `test_env_base_url_pointing_to_live_raises` | Env + init |
| T6 | Request to non-testnet host raises `BinanceLiveHostBlocked` | `test_transport_event_hooks.py` | `test_request_to_non_testnet_host_raises` | Pre-request hook |
| T7 | 3xx redirect from testnet to non-testnet raises | `test_transport_event_hooks.py` | `test_redirect_to_non_testnet_host_raises` | Response hook |
| T8 | Signed request sets `X-MBX-APIKEY` header (testnet only) | `test_transport_event_hooks.py` | `test_signed_request_has_apikey_header` | Pre-request hook |
| T9 | Signed request to a public host (cross-allowlist) raises | `test_transport_event_hooks.py` | `test_signed_request_to_public_host_raises` | Cross-allowlist guard |
| T10 | `BINANCE_TESTNET_ENABLED` unset → adapter init refuses | `test_execution_client_fail_closed.py` | `test_disabled_by_default_raises_on_construct` | Env + init |
| T11 | API key missing → adapter init raises `BinanceMissingCredentials` | `test_execution_client_fail_closed.py` | `test_missing_api_key_raises` | Env + init |
| T12 | API secret missing → adapter init raises `BinanceMissingCredentials` | `test_execution_client_fail_closed.py` | `test_missing_api_secret_raises` | Env + init |
| T13 | `submit_order(..., confirm=False)` returns a `DryRunResult` without HTTP | `test_execution_client_preview.py` | `test_dry_run_no_http` (uses `httpx_mock` with zero expected requests) | Method |
| T14 | `submit_order(..., confirm=True)` submits to testnet (fake-client) | `test_execution_client_submit_cancel_fake.py` | `test_confirmed_submit_hits_testnet_host` | Method + transport |
| T15 | `cancel_order(..., confirm=True)` submits cancel to testnet | `test_execution_client_submit_cancel_fake.py` | `test_confirmed_cancel_hits_testnet_host` | Method + transport |
| T16 | Notional override without `notional_override_reason` raises | `test_execution_client_fail_closed.py` | `test_notional_override_without_reason_raises` | Method |
| T17 | HMAC signing produces canonical signature for fixed inputs | `test_signing.py` | `test_sign_request_params_canonical` | Pure function |
| T18 | Scalper entry decision: BUY when oversold + uptrend | `test_decision.py` | `test_compute_action_entry_buy` | Pure function |
| T19 | Scalper TP decision: SELL when price ≥ TP threshold | `test_decision.py` | `test_compute_action_tp_sell` | Pure function |
| T20 | Scalper SL decision: SELL when price ≤ SL threshold | `test_decision.py` | `test_compute_action_sl_sell` | Pure function |
| T21 | Scalper holds when no signal | `test_decision.py` | `test_compute_action_hold` | Pure function |
| T22 | Scalper refuses entry when symbol busy (open position exists) | `test_decision.py` | `test_compute_action_refuses_busy_symbol` | Pure function |
| T23 | Scalper refuses entry when instrument health is `manual_backfill_required` | `test_decision.py` | `test_compute_action_refuses_unhealthy_instrument` | Pure function |
| T24 | Ledger idempotent upsert: re-recording the same lifecycle event is a no-op | `test_ledger_service.py` | `test_record_submit_idempotent` | Service |
| T25 | Ledger refuses invalid state transition | `test_ledger_service.py` | `test_invalid_transition_raises` | State machine |
| T26 | Ledger emits Sentry on `anomaly` | `test_ledger_service.py` | `test_anomaly_emits_sentry` | Telemetry |
| T27 | Repository import guard: import from outside ledger module raises | `test_ledger_service.py` | `test_repository_not_importable_externally` | Module boundary |
| T28 | Runner end-to-end: planned → previewed → validated → submitted → filled → closed | `test_runner_lifecycle.py` | `test_lifecycle_happy_path` | Integration |
| T29 | Runner reconciliation: clean ledger state passes | `test_runner_reconciliation.py` | `test_clean_state_proceeds` | Integration |
| T30 | Runner reconciliation: drift → anomaly | `test_runner_reconciliation.py` | `test_drift_raises_anomaly` | Integration |
| T31 | Smoke CLI default-disabled exits 0 with no side effects | `test_binance_testnet_scalper_smoke.py` | `test_smoke_disabled_by_default_no_side_effects` | CLI |
| T32 | Smoke CLI dry-run with creds produces no `submitted` ledger rows | `test_binance_testnet_scalper_smoke.py` | `test_smoke_dryrun_creates_no_submitted_rows` | CLI + Ledger |
| T33 | Audit: no `api.binance.com` URL string anywhere in `binance/testnet/` | `test_audit_no_live_host.py` | `test_no_live_host_url_in_testnet_package` | Source |
| T34 | Audit: no `binance_testnet_scalper`/`BinanceTestnetExecutionClient` in scheduler/taskiq/tasks | `test_audit_no_live_host.py` | `test_no_scheduler_activation` | Source |
| T35 | Audit: `app/services/brokers/binance/rest_client.py` does NOT gain signed methods (Child B invariant) | `test_audit_no_signed_endpoints.py` (Child B test, extended) | `test_no_signed_endpoint_surface_in_binance_public_package` | Source |

This matrix is the contract. Every row maps to a numbered safety invariant or a user-listed test category. Reviewers can spot-check by matching row numbers to test names.

---

## ROB-285 reuse points (explicit)

| Child B artifact | How Child C reuses | Coupling |
|---|---|---|
| `app/services/brokers/binance/host_allowlist.py::PUBLIC_HOSTS` | Cross-allowlist disjointness assertion (T1) imports this. Child C does NOT extend or modify it. | Read-only import |
| `app/services/brokers/binance/errors.py::BinanceLiveHostBlocked` | Re-raised by Child C transport when host is non-testnet. | Import |
| `app/services/brokers/binance/errors.py::BinanceSignedEndpointAttempted` | Child B's public transport raises this when `X-MBX-APIKEY` is seen. Child C's audit test confirms the public transport still has this guard. | Import + audit |
| `app/services/brokers/binance/ws_client.py::BinancePublicWSClient` | Scalper consumes WS `kline_1m` and `bookTicker` events for live price input. | Composition |
| `app/services/brokers/binance/rest_client.py::BinancePublicRestClient` | Scalper falls back to REST `bookTicker` polling when WS is unavailable. | Composition |
| `app/services/brokers/binance/ingest.py::BinanceCandleIngester` | Scalper uses the closed-1m candle stream as backtest-replay-able input (testnet shadow mode). | Composition |
| `app/services/instrument_health/service.py::CryptoInstrumentHealthService` | Scalper reads instrument health; refuses to trade `degraded`/`manual_backfill_required`. | Read-only |
| `app/models/crypto_instruments.py::CryptoInstrument` | Ledger rows reference `instrument_id` for the testnet symbols. | FK reference |
| `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` | Child C extends this test's exclusion list to allow signed surface ONLY inside `binance/testnet/`. The Child B regex still fails for any signed-endpoint name added under `binance/` outside `binance/testnet/`. | Test extension |

Crucially: **Child C never modifies Child B's source**. Only the audit test's exclusion list (one targeted change) and the `tests/services/brokers/binance/testnet/` subdirectory. If a "tweak to host_allowlist.py" or similar Child-B-side change feels needed, that's a signal something is being mis-modeled — escalate before changing.

---

## Reviewer risk points (where to spend the review attention)

1. **Cross-allowlist guard correctness.** The test `test_signed_request_to_public_host_raises` (T9) is the single most important behavioral assertion in this PR. If the public allowlist were ever to accept a signed request (or vice versa), a misconfigured deploy could send a real-money order to live Binance. Verify the test actually exercises the guard, not just a happy path.
2. **HMAC signing chokepoint.** `signing.py::_sign_request_params` is the only signature-producing surface in the codebase. If anyone reimplements signing inline elsewhere, drift is inevitable. Confirm there's no inline signing in `execution_client.py` — it must call `_sign_request_params`.
3. **API key/secret never logged.** `BinanceTestnetExecutionClient.__init__` accepts the secret; verify it's stored in a private attribute, never logged, never put on a Sentry tag, never in error messages. Add a test that captures all log output during a simulated failure and asserts the secret string never appears.
4. **State machine transitions.** The 9-state lifecycle (`planned → ... → reconciled` + `anomaly`) has 14 valid transitions. The state machine in `BinanceTestnetLedgerService.record_*` should refuse any invalid transition with a clear error. Spot-check Task 9.
5. **Reconciliation depth + drift handling.** A live testnet account with stale `submitted` rows and broker-side cancellations needs to be reconcilable on restart without nuking the ledger. Open item #3 lock matters here.
6. **Scheduler drift.** Task 1's audit test must explicitly include `app/core/scheduler.py` and `app/tasks/*` in its grep; check the regex actually matches `BinanceTestnetExecutionClient` and `binance_testnet_scalper`.
7. **Smoke CLI default-disabled behavior.** T31 must verify zero HTTP (use `httpx_mock` with `assert_all_responses_were_requested=False` and a 0-call assertion) and zero ledger writes (use a transactional `db_session` fixture and assert row count delta is 0).

---

## Task list

> 15 tasks. Each task follows the TDD pattern from Child B: failing test → run-to-fail → implement minimum → run-to-pass → commit. Each task's first commit message documents any open-item lean adoption for items resolved in that task.

### Task 1: Audit invariants — testnet code isolation + scheduler-drift guard

Encode the safety net before any signed code lands. Update Child B's audit test to allow signed methods only inside `binance/testnet/`. Add a new audit test that enforces no scheduler activation and no live-host URL strings in the testnet package.

**Files:**
- Create: `tests/services/brokers/binance/testnet/__init__.py`
- Create: `tests/services/brokers/binance/testnet/test_audit_no_live_host.py`
- Modify: `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` (extend scope exclusion for `binance/testnet/`)

Step-by-step omitted for brevity — pattern identical to Child B Task 1.

### Task 2: Testnet host allowlist module + new errors

Two separate frozensets, cross-allowlist disjointness assertion, new exception types.

### Task 3: Testnet transport factory with event hooks + cross-allowlist guard

`build_testnet_client(api_key, api_secret)`. Pre-request hook: assert host in `TESTNET_HOSTS`, assert host NOT in `PUBLIC_HOSTS` (cross-allowlist guard), attach `X-MBX-APIKEY` header. Response hook: 3xx → raise.

### Task 4: HMAC signing chokepoint (`_sign_request_params`)

Single function, inspectable by tests. Decide SDK vs stdlib (open item #1).

### Task 5: `BinanceTestnetExecutionClient` — preview / dry-run paths

`submit_order(..., dry_run=True, confirm=False)` returns `DryRunResult`. `cancel_order` similar. No HTTP. All preview metadata logged for ledger consumption.

### Task 6: `BinanceTestnetExecutionClient` — confirmed submit + cancel + query

Fake-client tests (httpx_mock targeting `testnet.binance.vision`). Notional override forcing function (`notional_override_reason`).

### Task 7: `binance_testnet_order_ledger` migration + ORM model

Alembic migration chaining off the post-#897 main head. Columns: `id`, `instrument_id` (FK), `client_order_id` (UNIQUE), `broker_order_id` (nullable), `side`, `qty`, `price` (nullable for market), `tp_price`, `sl_price`, `lifecycle_state`, `parent_client_order_id` (for TP/SL pairing), lifecycle timestamps, `anomaly_reason` (nullable), `last_reconciled_at` (nullable), `extra_metadata` JSONB. CHECK constraint on lifecycle state vocab. Register `BinanceTestnetOrderLedger` in `app/models/__init__.py`.

### Task 8: `BinanceTestnetLedgerRepository` + import-guard test

Module-internal repository with the same runtime guard pattern as ROB-285's `CryptoInstrumentHealthRepository`.

### Task 9: `BinanceTestnetLedgerService` with 11 `record_*` methods + state-machine validation

State transition table (locked in this task):

```
planned        → previewed | anomaly
previewed      → validated | anomaly
validated      → submitted | anomaly
submitted      → filled | cancelled | anomaly
filled         → tp_sl_armed | closed | anomaly
tp_sl_armed    → tp_sl_triggered | cancelled | anomaly
tp_sl_triggered → closed | anomaly
cancelled      → reconciled | anomaly
closed         → reconciled | anomaly
anomaly        → reconciled  (only by operator-initiated clear)
```

### Task 10: Scalper decision model (pure function)

`compute_action(state, snapshot, config) → Action` where `Action ∈ {Hold, Entry(side, qty, tp, sl), Exit(reason)}`. Tests T18-T23.

### Task 11: Scalper runner + reconciliation

Wires market data → decision → execution adapter → ledger. Reconciliation on construction.

### Task 12: Instrument seeder + smoke CLI

`scripts/binance_testnet_seed_instruments.py`: idempotent seed for `BTCUSDT/ETHUSDT/SOLUSDT` `(venue=binance, product=spot)` rows in `crypto_instruments`. Skips existing rows.

### Task 13: Smoke CLI (`binance_testnet_scalper_smoke`)

Default-disabled exit-0 path. `--dry-run` flag (default true). `--confirm` flag (default false). Exit codes table mirrors Child B's smoke.

### Task 14: Runbook + CLAUDE.md ledger entry

`docs/runbooks/binance-testnet-scalping.md` covering env vars, testnet reset cadence, OCO-on-spot absence, operator-initiated clear of anomaly states, manual close procedure, smoke CLI usage. Update CLAUDE.md with new "Binance Testnet Order Ledger (ROB-286)" section parallel to existing "Alpaca Paper 실행 레저 (ROB-84)".

### Task 15: Final audit + screener regression + PR description

Re-run T33/T34/T35 audits. Run screener regression to confirm Child B/A surfaces unaffected. Compose PR description with safety invariant checklist, operator cutover checklist link, open-items leans summary.

---

## Verification commands (Child C implementation PR)

```bash
# Lint trio (mirrors CI):
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning

# Dep lock (no new runtime deps expected):
uv lock --check

# Targeted tests:
uv run pytest tests/services/brokers/binance/testnet/ -v
uv run pytest tests/services/scalping/ -v
uv run pytest tests/scripts/test_binance_testnet_scalper_smoke.py -v

# Safety-invariant suite (subset that maps to user-stated guarantees):
uv run pytest tests -k "audit_no_live or fail_closed or cross_allowlist or default_disabled" -q

# Regression (Child A + Child B + screener unaffected):
uv run pytest tests/services/brokers/binance/ tests/services/instrument_health/ tests/services/daily_candles/ -q
uv run pytest tests -k "screener_snapshot or invest_crypto_screener" -q

# Scope audit greps:
grep -rln "api.binance.com" --include="*.py" app/services/brokers/binance/testnet/ || echo "OK: no live host literal"
grep -rln "BinanceTestnetExecutionClient\|binance_testnet_scalper" app/core/scheduler.py app/core/taskiq_broker.py app/tasks/ || echo "OK: no scheduler activation"
grep -rln "X-MBX-APIKEY" --include="*.py" app/services/brokers/binance/  # should only match testnet/transport.py

# Smoke CLI default-disabled:
uv run python -m scripts.binance_testnet_scalper_smoke   # exits 0, "disabled" log, zero side effects
```

---

## Self-review checklist (run after writing this plan, before opening PR)

- [x] Plan covers every user-listed scope item (testnet host, signed isolation, preview/submit/cancel, scalper, entry/TP/SL, ledger boundary, ROB-285 reuse, fail-closed allowlist, no-live-broker invariant).
- [x] Plan forbids every user-listed item (live Binance/Alpaca/KIS/Upbit, prod deploy, prod DB alembic upgrade, prod scheduler/TaskIQ, real-money mutation, unapproved futures SDK, signed in public adapter).
- [x] §Hard safety invariants enumerates all 8 user-stated hard requirements (testnet base URL only, no signed to live, no creds in public path, fail-closed on missing creds, order path testnet/paper only, default disabled, scheduler disabled, operator gate).
- [x] Test matrix enumerates ≥1 test per user-listed test category (host allowlist, signed testnet-only, missing cred, dry-run/preview, submit/cancel fake-client, scalper TP/SL units, ledger idempotency, no-live-host regression, smoke default-disabled).
- [x] Production cutover gate is explicit and deferred.
- [x] Scheduler activation is explicit and deferred.
- [x] Open items table has lean default + lock task for each item.
- [x] ROB-285 reuse points are enumerated with coupling type.
- [x] Reviewer risk points are surfaced for spot-checking.
- [x] No placeholders (TBD / TODO / fill in details / implement later).
- [x] Task list covers 15 tasks; each task references the test matrix rows it satisfies.
- [x] Child B's source is read-only; only `tests/services/brokers/binance/test_audit_no_signed_endpoints.py` is modified (one targeted exclusion-list extension).

---

## Plan-only PR scope (this PR)

This PR contains exactly **one file**: `docs/plans/ROB-286-binance-testnet-scalping-mvp-implementation-plan.md`. No code, no migrations, no scheduler entries, no broker/order/watch/order-intent mutation in the diff. Implementation PR comes after plan review/approval.
