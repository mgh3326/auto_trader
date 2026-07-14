# ROB-849 Paper Cohort Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build immutable BTC/ETH paper-validation cohorts with canonical Binance public snapshots, deterministic cross-venue signals, default-off TaskIQ execution, and exactly-once links to existing Binance Demo and Alpaca Paper ledgers.

**Architecture:** A new `paper_cohort` domain owns immutable research rows and pure snapshot/signal contracts. The runner claims work in PostgreSQL, persists the canonical signal before venue evidence, and enters brokers only through the ROB-845 application with a verifier that calls ROB-848 authorization. Existing native ledgers remain lifecycle truth.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy async, PostgreSQL/Alembic, TaskIQ, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- Canonical source is exactly `binance_public_spot` at `https://api.binance.com`.
- Canonical universe is exactly `BTCUSDT`, `ETHUSDT`; interval is exactly `1m`.
- V1 venues are exactly Binance Spot Demo and Alpaca Crypto Paper, spot, leverage 1x.
- Binance capability remains buy/market/notional; Alpaca remains buy-or-sell/limit/qty.
- Broker mutation enters only through `build_paper_execution_application(verifier=...)` and `PaperOrderRequest`.
- `PAPER_COHORT_ENABLED` is default-off; disabled means zero schedules and zero new intents.
- No actual credentials, `.env`, live account, or real broker order may be used.
- Every production behavior follows a witnessed red test before implementation.

---

### Task 1: Immutable schema and single-head migration

**Files:**
- Create: `app/models/paper_cohort.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/20260714_rob849_paper_cohort.py`
- Create: `tests/services/paper_cohort/test_models.py`
- Create: `tests/services/paper_cohort/test_migration.py`
- Create: `tests/services/paper_cohort/conftest.py`

**Interfaces:**
- Produces ORM types `PaperValidationCohort`, `PaperValidationCohortAssignment`, `CanonicalMarketSnapshot`, `PaperCohortDecision`, `PaperCohortVenueIntent`, `PaperCohortRunClaim`, and `PaperRunOrderLink`.
- Migration revision is `20260714_rob849_paper_cohort`, down revision `20260713_rob848_paper_validation`.

- [ ] **Step 1: Write failing metadata and schema tests**

Assert exact table names, schema `research`, cohort/assignment role/venue/symbol/leverage checks, unique snapshot and intent identities, link column allowlist, and absence of fill/status/price/fee/P&L fields.

- [ ] **Step 2: Run the model tests and witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_models.py -q`

Expected: collection fails because `app.models.paper_cohort` does not exist.

- [ ] **Step 3: Add ORM models and exports**

Use JSONB for exact ordered venue/symbol/weights/payload/evidence, `Numeric` for capital, timezone-aware timestamps, and named constraints. Only `PaperCohortRunClaim` exposes mutable lease/result columns.

- [ ] **Step 4: Add migration and database immutability triggers**

Create all seven tables. Add deferred composition validation for one champion and at most two challengers, append-only UPDATE/DELETE/TRUNCATE rejection for audit tables, experiment/run foreign keys, and single-column/index names through `op.f(...)` where naming convention requires it.

- [ ] **Step 5: Run schema tests and migration round trip**

Run: `uv run pytest tests/services/paper_cohort/test_models.py tests/services/paper_cohort/test_migration.py -q`

Expected: all pass, including actual PostgreSQL upgrade → downgrade → upgrade and `uv run alembic heads` returning one head.

- [ ] **Step 6: Commit**

Commit: `feat(ROB-849): add immutable paper cohort schema`

### Task 2: Frozen contracts and cohort activation

**Files:**
- Create: `app/services/paper_cohort/__init__.py`
- Create: `app/services/paper_cohort/contracts.py`
- Create: `app/services/paper_cohort/cohort_service.py`
- Create: `tests/services/paper_cohort/test_cohort_service.py`

**Interfaces:**
- Produces frozen DTOs `CohortActivation`, `CohortAssignmentInput`, `RunMode`, `PaperCohortError`.
- Produces `PaperCohortService.activate(request) -> PaperValidationCohort`.

- [ ] **Step 1: Write failing activation matrix tests**

Cover exact venues/symbols, spot/leverage 1, one champion, challenger limit, mapping reuse, matching ROB-846 experiment/run/version/hash identity, matching latest ROB-848 cohort identity, replay, and idempotency conflict.

- [ ] **Step 2: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_cohort_service.py -q`

Expected: import failure for the new contracts/service.

- [ ] **Step 3: Implement frozen validation and transactional activation**

Normalize no caller values: require exact tuples. Compute `cohort_hash = canonical_sha256()` over the complete frozen cohort plus ordered assignments. Lock the cohort ID with `pg_advisory_xact_lock`, validate ROB-846 rows and latest ROB-848 rows, insert once, and return the stored row only for byte-identical replay.

- [ ] **Step 4: Verify GREEN and commit**

Run: `uv run pytest tests/services/paper_cohort/test_cohort_service.py tests/services/research/test_strategy_experiment_registry.py tests/services/paper_validation/test_service_transitions.py -q`

Commit: `feat(ROB-849): activate frozen paper cohorts`

### Task 3: Canonical snapshot capture and hashing

**Files:**
- Create: `app/services/paper_cohort/market_snapshot.py`
- Create: `tests/services/paper_cohort/test_market_snapshot.py`
- Create: `tests/services/paper_cohort/test_snapshot_safety.py`

**Interfaces:**
- Produces `CanonicalSnapshotPayload`, `SnapshotCaptureRequest`, `CanonicalSnapshotCapture.capture(request) -> CanonicalSnapshotPayload`.
- Consumes only `BinancePublicRestClient`, `BinanceKlineRow`, and `BinanceBookTicker` from the unsigned public boundary.

- [ ] **Step 1: Write failing happy-path/hash/JSONB tests**

Use fixed UTC clocks and DTO fixtures. Assert exact source/host/symbol/interval, ordered payload, reproducible hash, and JSON encode/decode hash equality.

- [ ] **Step 2: Write failing fail-close matrix**

Parametrize open candle, short lookback, gap, duplicate, unsorted series, partial symbol/ticker, stale/skewed ticker, non-aware timestamps, NaN/Infinity/non-positive fields, OHLC inconsistency, crossed book, and provider exception.

- [ ] **Step 3: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_market_snapshot.py tests/services/paper_cohort/test_snapshot_safety.py -q`

Expected: import failure for `market_snapshot`.

- [ ] **Step 4: Implement all-or-nothing capture**

Request `end_time = floor(capture_started_at, minute) - 1 microsecond`, exact lookback, and interval `1m`. Build no return object until both symbol candle and ticker sets validate. Encode decimals as strings and UTC timestamps as ISO strings before hashing.

- [ ] **Step 5: Verify GREEN and public-client regression**

Run: `uv run pytest tests/services/paper_cohort/test_market_snapshot.py tests/services/paper_cohort/test_snapshot_safety.py tests/services/brokers/binance/test_rest_client.py -q`

Commit: `feat(ROB-849): capture canonical Binance snapshots`

### Task 4: Deterministic signals and shadow runner

**Files:**
- Create: `app/services/paper_cohort/signals.py`
- Create: `app/services/paper_cohort/runner.py`
- Create: `tests/services/paper_cohort/test_signals.py`
- Create: `tests/services/paper_cohort/test_runner_shadow.py`

**Interfaces:**
- Produces `CanonicalTargetSignal` and `compute_target_signal(snapshot, assignment, symbol)`.
- Produces `PaperCohortRunner.run(invocation) -> CohortRunResult` with injected capture, venue-quote provider, application factory, clock, and session factory.

- [ ] **Step 1: Write failing signal determinism tests**

Assert same snapshot/version yields byte-identical signal/hash for different venue quote fixtures. Assert no quote provider call occurs until the signal has been computed and persisted.

- [ ] **Step 2: Write failing shadow tests**

Assert deterministic would-order/idempotency evidence, zero application construction/calls, zero native resolver calls, and zero links. Capture failure must leave snapshots, signals, venue intents, quotes, application, and native ledgers at zero.

- [ ] **Step 3: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_signals.py tests/services/paper_cohort/test_runner_shadow.py -q`

- [ ] **Step 4: Implement pure signal then shadow orchestration**

Persist the canonical snapshot, then each pre-rounding signal, then request venue evidence. Build Binance buy/market/notional and Alpaca buy/limit/qty would-orders without changing the signal payload/hash. Return stable `unsupported_capability` for any invalid conversion.

- [ ] **Step 5: Verify GREEN and commit**

Run: `uv run pytest tests/services/paper_cohort/test_signals.py tests/services/paper_cohort/test_runner_shadow.py -q`

Commit: `feat(ROB-849): add deterministic shadow cohort runner`

### Task 5: Production provenance, paper-active submit, and native links

**Files:**
- Create: `app/services/paper_cohort/provenance.py`
- Create: `app/services/paper_cohort/native_links.py`
- Modify: `app/services/paper_cohort/runner.py`
- Create: `tests/services/paper_cohort/test_provenance.py`
- Create: `tests/services/paper_cohort/test_runner_paper_active.py`

**Interfaces:**
- Produces `PaperCohortProvenanceVerifier.verify(PaperOrderRequest) -> VerifiedExperimentProvenance`.
- Produces cohort-backed ROB-848 `FrozenInputHashProvider` and `PolicyHashProvider` implementations.
- Produces `NativeOrderResolver.resolve(venue, client_order_id) -> NativeOrderIdentity`.

- [ ] **Step 1: Write failing verifier mismatch tests**

Cover cohort, assignment, experiment, strategy version/hash, config/policy/input, snapshot ID/hash/source/as-of, decision, intent, stopped cohort, and ROB-848 state mismatch. Spy registry/adapter/broker/native ledger calls must stay zero.

- [ ] **Step 2: Write failing paper-active success/replay tests**

Inject a fake ROB-845 application around the real verifier. Assert the runner builds `PaperOrderRequest`, calls submit once, resolves an existing native row, and stores only the thin link fields. Replay returns the same link/result.

- [ ] **Step 3: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_provenance.py tests/services/paper_cohort/test_runner_paper_active.py -q`

- [ ] **Step 4: Implement verifier and application composition**

Build `ValidationIdentity` from persisted assignment/cohort rows, call `PaperValidationService.authorize_order_submission`, require returned state exactly `paper_active`, then return trusted decision/reference evidence. Runner must call only `build_paper_execution_application(verifier=verifier).submit(request)`.

- [ ] **Step 5: Implement native resolution and thin-link persistence**

Resolve Binance `BinanceDemoOrderLedger` and Alpaca `AlpacaPaperOrderLedger` by returned client order ID. Reject missing/mismatched broker ID. Store one link under unique decision/venue and native-row constraints.

- [ ] **Step 6: Verify GREEN and adjacent adapter suites**

Run: `uv run pytest tests/services/paper_cohort/test_provenance.py tests/services/paper_cohort/test_runner_paper_active.py tests/services/brokers/paper tests/services/brokers/binance/paper tests/services/brokers/alpaca/paper -q`

Commit: `feat(ROB-849): submit verified cohort paper orders`

### Task 6: Cross-session claims and crash recovery

**Files:**
- Modify: `app/services/paper_cohort/runner.py`
- Create: `tests/services/paper_cohort/test_runner_concurrency.py`
- Create: `tests/services/paper_cohort/test_runner_recovery.py`

**Interfaces:**
- Run claims use `(cohort_id, run_id, round_decision_id)` uniqueness, request hash, owner token, lease expiry, completion result, and compare-and-swap takeover.

- [ ] **Step 1: Write failing actual PostgreSQL barrier tests**

Use two independent async sessions and a barrier. Assert one snapshot, one decision/venue intent, one application submit, one native order, and one link. Conflicting request hashes return stable `invocation_conflict`.

- [ ] **Step 2: Write failing crash-after-submit test**

Raise from an injected hook after application success and before link persistence. Advance the clock past the claim lease and retry. Assert ROB-845/native idempotency returns the original order, broker POST count remains one, and one link is created.

- [ ] **Step 3: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_runner_concurrency.py tests/services/paper_cohort/test_runner_recovery.py -q`

- [ ] **Step 4: Implement claim/replay/takeover transitions**

Use PostgreSQL insert-on-conflict and conditional update against the observed owner/expiry. Persist immutable downstream rows under their unique constraints and translate constraint races into replay or stable conflict results.

- [ ] **Step 5: Verify GREEN repeatedly and commit**

Run: `uv run pytest tests/services/paper_cohort/test_runner_concurrency.py tests/services/paper_cohort/test_runner_recovery.py -q -n 2` three times.

Commit: `feat(ROB-849): make cohort runs exactly once`

### Task 7: Default-off TaskIQ composition and safety guards

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/core/taskiq_broker.py`
- Create: `app/jobs/paper_cohort.py`
- Create: `app/tasks/paper_cohort_tasks.py`
- Create: `tests/services/paper_cohort/test_taskiq.py`
- Create: `tests/services/paper_cohort/test_ast_safety.py`

**Interfaces:**
- Adds `PAPER_COHORT_ENABLED: bool = False`, `PAPER_COHORT_CRON`, and task actor configuration.
- Produces `paper_cohort.run_active` TaskIQ task and `run_active_paper_cohorts()` job.

- [ ] **Step 1: Write failing default-off and AST tests**

Assert false default, empty schedule labels, no DB/job call while false, and no Demo/ROB-838/signed/live/raw-submit/MCP mutation imports from canonical capture. Assert no duplicate profile, port, or capability registry declarations.

- [ ] **Step 2: Witness RED**

Run: `uv run pytest tests/services/paper_cohort/test_taskiq.py tests/services/paper_cohort/test_ast_safety.py -q`

- [ ] **Step 3: Add thin task and job composition**

The decorator schedule helper returns `[]` while disabled. A direct disabled task invocation audits only already prepared incomplete claims in recovery-only mode. For enabled cohorts, the job derives stable UTC minute-bucket identities for shadow observations and one cohort-hash-bound identity for paper-active target allocation, creates the public client and ROB-845 application only for new enabled work, and closes resources.

- [ ] **Step 4: Verify GREEN and TaskIQ regression**

Run: `uv run pytest tests/services/paper_cohort/test_taskiq.py tests/services/paper_cohort/test_ast_safety.py tests/test_taskiq_broker.py -q`

Commit: `feat(ROB-849): add default-off cohort task`

### Task 8: Full verification, independent review, PR, and CI

**Files:**
- Modify only files required by witnessed review failures.
- Update: `docs/superpowers/specs/2026-07-14-rob-849-paper-cohort-design.md` if final interfaces differ.

- [ ] **Step 1: Run focused and adjacent suites**

Run all `tests/services/paper_cohort`, `tests/services/brokers/paper`, Binance/Alpaca production adapter suites, and ROB-845/846/847/848 focused suites. Record pass counts.

- [ ] **Step 2: Run migration and static gates**

Run actual PostgreSQL upgrade → downgrade → upgrade, `uv run alembic heads`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and `git diff --check origin/main..HEAD`.

- [ ] **Step 3: Run broad non-live regression**

Run: `make test`

Expected: no new failures; existing warnings are documented separately.

- [ ] **Step 4: Obtain independent spec/code/security/concurrency reviews**

Dispatch four read-only reviewers. For each actionable blocker, first add a failing regression test, witness RED, implement the minimal fix, rerun GREEN, and commit a review-fix commit. Do not change code for non-reproducible comments.

- [ ] **Step 5: Verify clean diff and push**

Push branch `rob-849`, create a PR against `main` linked to ROB-849, and keep it open.

- [ ] **Step 6: Monitor CI and review**

Wait for every required check. Fix failures test-first, push follow-ups, and leave Linear `In Progress` and the PR `OPEN`.

- [ ] **Step 7: Record Linear completion evidence**

Comment base/ROB-848 SHA, red→green evidence, per-suite pass counts, migration/head state, final commit SHA, PR URL, and remaining risks.

### Task 9: Pre-merge concurrency, lineage, and operations hardening

**Files:**
- Modify: `app/models/paper_cohort.py`
- Modify: `alembic/versions/20260714_rob849_paper_cohort.py`
- Modify: `tests/_schema_bootstrap.py`
- Modify: `app/services/paper_cohort/cohort_service.py`
- Modify: `app/services/paper_cohort/runner.py`
- Modify: `app/services/paper_cohort/order_control.py`
- Modify: `app/jobs/paper_cohort.py`
- Modify: PAPER_EXECUTION operator tooling and contract documentation
- Add/modify: focused PostgreSQL, runner recovery, quote freshness, and kill-switch tests

- [ ] **Step 1: Witness the nine review failures with regression tests**

Cover fresh/retry ordering, per-intent durable links, same-run/different-round isolation, activation/state serialization, one-shot target reservations, future activation, stale/future venue quotes, cross-table lineage, and durable stop/re-enable behavior.

- [ ] **Step 2: Strengthen immutable lineage and activation serialization**

Add exact composite foreign keys and venue/ledger checks. Lock sorted/deduplicated validation IDs and then the cohort before activation state reads. Runner checkpoints prepend their claim-row lock to the same suffix. Verify with real two-session PostgreSQL barriers and invalid cross-wire inserts.

- [ ] **Step 3: Make execution order and checkpoints deterministic**

Load prepared intents by assignment ordinal, cohort symbol order, and venue order for the exact round. Commit each resolved native link independently, and reacquire/revalidate claim, stop, activation, authoritative state, and provenance before every subsequent adapter call.

- [ ] **Step 4: Enforce one-shot target allocation**

Persist a pre-send target reservation unique by cohort/assignment/symbol/venue. Allow only the reservation's originating intent to retry; later rounds complete as no-mutation observations.

- [ ] **Step 5: Add an operational stop boundary**

Expose an authenticated operator-only PAPER_EXECUTION entrypoint that commits an immutable cohort stop fence before cleanup, then idempotently cancels/closes only linked cohort-owned paper orders and returns resumable per-link outcomes. Runner and scheduler must honor the fence even after feature flags are re-enabled.

- [ ] **Step 6: Close temporal and documentation gaps**

Reject direct pre-activation runs and stale/future venue quotes. Correct disabled-job and rollback documentation to match recovery-audit behavior.

- [ ] **Step 7: Verify, independently re-review, push, and merge**

Run migration upgrade/downgrade/upgrade, focused and adjacent suites, full non-live regression, Ruff/format/ty/security/diff checks, independent SQL/concurrency/security reviews, all PR checks, and only then merge PR #1532 and mark ROB-849 Done.
