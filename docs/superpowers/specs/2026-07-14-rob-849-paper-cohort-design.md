# ROB-849 Paper Cohort Design

## Status and authority

This design implements the approved ROB-849 Linear body and its canonical-source decision comment. It consumes ROB-845 through `build_paper_execution_application(verifier=...)` and `PaperOrderRequest`, and consumes ROB-848 through `PaperValidationService.authorize_order_submission`. It does not add another actor role, validation state machine, paper-execution profile, broker port, capability, or native order lifecycle.

## Scope

V1 supports exactly BTCUSDT and ETHUSDT, Binance Spot Demo and Alpaca Crypto Paper, spot, leverage 1x, one champion, and at most two challengers. A cohort and its assignments are append-only after activation. Each assignment binds one ROB-846 experiment and source backtest run to exact strategy/config/policy/input hashes, target weights, and one ROB-848 validation stream.

ROB-850 P&L and soak evaluation are excluded. Native Binance and Alpaca ledgers remain the only lifecycle, fill, status, fee, and P&L authorities.

## Module boundaries

- `app/models/paper_cohort.py` owns cohort, assignment, snapshot, decision, venue-intent, run-claim, and thin native-link persistence.
- `app/services/paper_cohort/contracts.py` owns frozen DTOs and stable failure codes.
- `app/services/paper_cohort/cohort_service.py` validates and atomically activates immutable cohorts against ROB-846 rows and the latest ROB-848 identities.
- `app/services/paper_cohort/market_snapshot.py` imports only `BinancePublicRestClient` DTO contracts. It captures and validates canonical public live Spot data and cannot import Demo, ROB-838, signed, live mutation, or MCP modules.
- `app/services/paper_cohort/signals.py` computes canonical pre-rounding signals from only the immutable snapshot and frozen assignment.
- `app/services/paper_cohort/provenance.py` implements the ROB-845 `ExperimentProvenanceVerifier`. It validates persisted cohort, assignment, decision, intent, snapshot, and ROB-846 identities, then invokes ROB-848 order authorization with cohort-backed frozen-input and policy providers.
- `app/services/paper_cohort/runner.py` owns atomic run claims, capture, signal persistence, post-signal venue evidence, shadow evidence, ROB-845 submission, native-row resolution, and thin-link persistence.
- `app/jobs/paper_cohort.py` is orchestration. `app/tasks/paper_cohort_tasks.py` is the default-off TaskIQ declaration.

The separate `paper_cohort` package is intentional: ROB-848 guards forbid ROB-849 concrete snapshot and broker dependencies inside `app/services/paper_validation`.

## Immutable persistence

All ROB-849 tables live in the `research` schema.

`paper_validation_cohorts` stores cohort ID/hash, exact venues and symbols, interval, lookback, capture skew/ticker-age limits, capital notional, spot/leverage constraints, activation/stop times, and creation time.

`paper_validation_cohort_assignments` stores assignment role/ordinal, ROB-848 validation identity, ROB-846 experiment/source-run/version identity, target weights, and exact strategy/config/policy/input hashes. Deferred PostgreSQL constraint triggers require one champion at ordinal zero and zero to two challengers at ordinals one and two.

`canonical_market_snapshots` stores schema/snapshot identity, cohort/run/round-decision identity, exact source `binance_public_spot`, exact host `https://api.binance.com`, capture timestamps, frozen settings, ordered payload, and content hash. A unique constraint permits one snapshot per cohort/run/round decision.

`paper_cohort_decisions` stores one byte-stable pre-rounding signal per assignment and symbol. `paper_cohort_venue_intents` stores at most one intent per decision and venue, including only request, would-order, risk/sizing/rounding provenance. These tables do not copy native lifecycle truth.

`paper_cohort_run_claims` is the mutable orchestration lease/result record. PostgreSQL unique constraints and compare-and-swap lease takeover replace process-local locks. An identical completed retry returns the persisted result; a different request hash fails with `invocation_conflict`; an unexpired competing lease fails with `invocation_in_progress`.

`paper_run_order_links` contains only cohort/run/decision/snapshot identity, venue, native ledger kind/row ID, client order ID, and broker order ID. It has no fill, lifecycle status, executed price, fee, or realized P&L columns.

PostgreSQL triggers reject UPDATE, DELETE, and TRUNCATE for cohort, assignment, snapshot, decision, venue-intent, and link audit rows. Run claims alone are mutable.

## Canonical snapshot and hash

One capture operation samples a timezone-aware start time, requests exactly the last fully closed window of `1m` candles for BTCUSDT and ETHUSDT through `BinancePublicRestClient`, fetches each symbol's `bookTicker`, and samples completion time. The request end time is the current minute boundary minus one microsecond, so an in-progress candle is neither requested nor accepted.

Validation is all-or-nothing. It rejects provider errors; any open candle; wrong interval/symbol/count; ordering, duplicate, or one-minute gap errors; timestamps outside the capture window; stale/skewed tickers; non-finite or non-positive numeric values; invalid OHLC semantics; crossed books; and partial symbol/ticker capture. Failure returns no snapshot object and the runner performs no signal, venue quote, application call, or native-ledger access.

The payload uses ordered symbol arrays, decimal strings, and UTC ISO-8601 timestamps. `canonical_sha256(payload)` is the content hash. The same JSONB payload read back from PostgreSQL must reproduce the same hash.

## Signal and venue evidence

For each assignment and symbol, the signal calculator uses only the frozen target weight, cohort capital notional, the canonical snapshot's last closed candle, and canonical book. It produces side, target weight, target notional, canonical reference price, and unrounded quantity, then hashes the canonical payload. The signal is persisted before any venue quote provider is called.

Binance converts the fixed target into buy/market/notional. Alpaca converts it into buy/limit/qty using post-signal venue quote evidence. Unsupported side, order type, sizing mode, cancel, or close is returned as `unsupported_capability`; no direct adapter or raw broker path is used. Venue evidence may affect risk, sizing, limit price, tick/lot rounding, and evidence only. It cannot mutate or re-hash the pre-rounding signal.

## Modes, state, and broker safety

`shadow` requires each assignment's authoritative ROB-848 state to be `shadow_soak`. It records deterministic would-order and idempotency evidence but never constructs the ROB-845 application, never calls submit/cancel/close, never resolves native ledgers, and never writes a native link.

`paper_active` persists the exact request and calls `build_paper_execution_application(verifier=production_verifier).submit(PaperOrderRequest(...))`. The verifier compares the caller request with the persisted intent, validates ROB-846 and snapshot identities, and calls ROB-848 `authorize_order_submission`. It additionally requires the returned authoritative state to be exactly `paper_active`, even though ROB-848 can authorize other lifecycle states for its own callers. State or hash mismatch therefore occurs before adapter resolution and yields zero adapter, broker, or native-ledger mutation.

After success, the runner resolves the returned client order ID against the existing Binance or Alpaca native ledger and writes one thin link. If the process crashes after broker success but before linking, a lease-expired retry submits the identical ROB-845 idempotency key. The native adapter recovers/replays broker truth without another POST, then the runner resolves and links the original row.

Stopped time, disabled `PAPER_COHORT_ENABLED`, or ROB-848 rejected/aborted state prevents new claims/intents. Cancellation is allowed only through the ROB-845 application for an already linked cohort-owned order and only where the existing venue capability allows it; no live account or raw client is reachable.

## TaskIQ and configuration

`PAPER_COHORT_ENABLED` defaults to false. The task decorator receives an empty schedule while false, and the task returns `disabled` before opening a DB session or creating a run claim. When enabled, a cron label invokes the job layer, which derives deterministic minute-bucket run/round IDs and runs active cohorts. The authenticated task actor ID must be present in the existing `PAPER_VALIDATION_ACTOR_ROLES` mapping with `system` or `operator`; otherwise paper-active authorization fails closed.

## Verification

Tests cover schema and activation constraints, immutable triggers, canonical hash and JSONB round-trip, every snapshot failure class, zero downstream calls on capture failure, byte-equivalent signals, venue call order, shadow zero mutation, paper-active state/hash mismatch, capability rejection, TaskIQ default-off behavior, retries, two-session barrier concurrency, crash recovery, thin-link columns, forbidden-import/call AST guards, migration round-trip/single head, and adjacent ROB-845/846/847/848 suites. Final gates include production adapter suites, broad non-live regression, Ruff check/format, ty, and `git diff --check origin/main..HEAD`.
