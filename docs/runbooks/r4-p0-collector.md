# R4 P0 no-regret collector

This is a manual, research-artifact-only point-in-time collector. It never
writes the production database and is not registered with TaskIQ, cron, or
Prefect. ROB-1108 adds epoch finalization, deadline recovery, independent
replica awareness, and fail-fast alerting without changing feature, score,
candidate, or stage-decision logic. DFC-4H 1.5 advances the collector to
`r4-p0-collector.v4`.

## Hard boundary

- Public unsigned market data only.
- REST: HTTPS `GET` only, host `fapi.binance.com`, exact paths:
  `/fapi/v1/openInterest`, `/fapi/v1/premiumIndex`,
  `/fapi/v1/premiumIndexKlines`,
  `/futures/data/openInterestHist`, `/futures/data/basis`,
  `/futures/data/takerlongshortRatio`.
- Websocket: `wss://fstream.binance.com/public` for book/depth and
  `wss://fstream.binance.com/market` for aggTrade/forceOrder. The split is
  required by Binance's post-2026-04-23 routed websocket contract.
- No API key, signing, account endpoint, order endpoint, or broker mutation.
- XRP/DOGE/SOL are signal symbols. BTC is collected only as a predictor.

## Collection priority and backfill boundary

Operational attention follows this order without dropping any P0 source:

1. The four websocket sources: aggTrade, forceOrder, 1-second bookTicker
   snapshots, and top-5 depth. Their local receive time is not recoverable
   later; book snapshots and forceOrder observations are likewise not
   reconstructable from a later REST backfill. Although aggTrade content was
   observed to be REST-backfillable through roughly 390 days, that does not
   recreate the original PIT receive-time observation.
2. `openInterestHist` at 5-minute resolution. Production probing confirmed
   that this endpoint has the actual short retrospective boundary (under 30
   days), so it is collected on every open-interest poll. The instantaneous
   `/fapi/v1/openInterest` snapshot remains as a separate additive source.
3. Basis, taker ratio, and premium/funding polls. They remain in scope, but are
   lower urgency because their exchange-time content can be backfilled. In
   particular, production basis rows were observed beyond 1,000 days; the
   earlier 30-day basis assumption is not used by this collector.

Backfill reach and PIT observation are separate properties. A later REST row
must not be treated as if it had the `local_receive_time` of the live collector.

The basis endpoint can transiently return HTTP 200 with an empty JSON list.
That is **missing source data**, not a successful zero-observation state. The
collector continues polling the other symbols, reports the failed symbol, and
re-reads a bounded 100-period (8h20m) window on the next attempt. Deduplication
keeps replays idempotent, while the original collection floor prevents this
recovery path from acting as seed backfill. Any absence still unresolved at
epoch finalization is `FINAL_MISSING`; two different canonical payloads for the
same source identity are `FINAL_CONFLICT`, never last-write-wins
`FINAL_COMPLETE`.

The premium-index 1-minute poll establishes its live floor with the latest
completed row. Every later poll requests the bounded range after the last
stored close through the current request time, capped by the active four-hour
contract window. A loop longer than 60 seconds therefore replays every missed
completed slot instead of keeping only the newest row. The epoch supervisor
remains the recovery path for a slot in the just-closed window.

## Modes

With no mode flag the command prints its contract and makes no network or disk
write:

```bash
uv run python -m scripts.r4_p0_collector
```

The bounded validation mode is explicit and capped at 180 seconds:

```bash
uv run python -m scripts.r4_p0_collector \
  --probe --duration 60 \
  --artifact-root /tmp/r4-p0-validation
```

The long-running operational arm requires both an environment gate and a CLI
flag:

```bash
export R4_P0_COLLECTOR_ENABLED=true
export R4_P0_ARTIFACT_ROOT="$HOME/work/herdr-artifacts/r4-p0-collector"
export R4_P0_COLLECTOR_ID="r4-p0-host-a"
export R4_P0_ALERT_WEBHOOK_URLS="https://alerts.example.invalid/r4"
uv run python -m scripts.r4_p0_collector --run \
  --replica-artifact /path/to/host-b/r4_p0_collector.sqlite3 \
  --minimum-healthy-replicas 2
```

Do not put these values in a committed `.env` file. Stop with SIGTERM or
Ctrl-C. A single-instance file lock prevents two writers sharing an artifact.
`--run` refuses to arm unless two distinct artifact paths are configured and
the minimum healthy replica count is at least two. It also requires an HTTPS
webhook unless the operator explicitly chooses `--allow-log-only-alerts` for a
manual validation. Webhook values are read from the environment, not command
arguments, so they are not exposed in the process list.

`--probe` remains a bounded single-replica validation and does not claim HA.

## Storage and restart contract

The artifact is `r4_p0_collector.sqlite3` in WAL mode with `synchronous=FULL`.
All persistence uses INSERT; database triggers reject UPDATE and DELETE.
`record_id` is a semantic unique key, so replayed websocket events and
unchanged REST periods are ignored after restart.

ROB-1108 and DFC-4H 1.5 add eight tables to the same local artifact. Every table has
UPDATE/DELETE rejection triggers:

- `epoch_source_events`: one immutable `OPEN` event per
  `(study, policy, source, symbol, decision_epoch)`.
- `collector_attempt_starts`: a pre-request row committed before network I/O;
  an unmatched start proves the process/host died mid-attempt.
- `collector_attempts`: the matching terminal row for each completed REST
  request or websocket connection attempt, including `attempted_at`, canonical
  request identity and its hash, response SHA-256 when a response exists, and
  terminal status. Failures also retain the exception class, message, formatted
  traceback, and a bounded response-body summary.
- `symbol_epoch_finalizations`: the one-shot three-way result and canonical
  evaluator input/hash.
- `late_only_corrections`: observations received after the deadline. These
  never rewrite a finalization.
- `collector_heartbeats`: append-only replica liveness evidence.
- `collector_process_versions`: one append-only startup stamp per run with the
  exact Git commit hash, collector version, and precommitted `t0_utc` loaded by
  that process.
- `collector_alert_events`: alert creation and each delivery result; failed
  delivery attempts are not overwritten.

The `source_manifest_hash` is computed from the sorted required-source matrix,
source schema and cadence requirements, and the three sealed signal symbols.
The policy hash is the R4.1 combined seal hash. A mismatch therefore cannot
silently reuse another finalization key.

`bookTicker` is stored as one snapshot per symbol per second (the P0 contract
allows either every change or 1s snapshots); raw aggTrade, forceOrder snapshots,
and top-5 depth updates are not sampled.

The premium poll stores the live mark/index/predicted-funding snapshot and every
completed 1-minute premium-index kline after the last stored close in the active
contract window. The first observation still stores only the latest completed
row, so this path cannot become seed backfill. An in-progress kline whose close
time is after `request_completed_at` is rejected, so downstream PIT consumers
cannot accidentally use its future close.

Each source/symbol/UTC-day partition is an ordered SHA-256 chain:
`partition_sha256` covers the previous chain head plus the current PIT
envelope. `raw_payload_sha256` independently covers canonical raw JSON.
A restart/reconnect marks the first subsequent stream record per source/symbol
with `gap_detected=true`; sequence discontinuities are also surfaced. The
`forceOrder` stream remains Binance's throttled snapshot, not a complete
liquidation tape.

Run the offline audit (no network) at any time:

```bash
uv run python -m scripts.r4_p0_collector \
  --audit --artifact-root "$R4_P0_ARTIFACT_ROOT"
```

It verifies raw hashes, partition-chain links, the 13 PIT column names, and
prints one secret-free sample per collected source. The collector also logs
session insert, duplicate, failure, and cumulative source counts every 30
seconds so a zero-row/partial-source run is visible. On graceful exit, the
process returns nonzero if any required source had neither an insert nor a
deduplicated response. `forceOrder` is reported separately as a sparse,
event-driven source and is not treated as failed merely because no liquidation
occurred during a short run.

## Deterministic epoch finalizer

Decision epochs are the six UTC four-hour boundaries. For decision epoch `e`,
the current source interval is `[e-4h,e)` and the immutable deadline is:

```text
finalize_at(e) = e + 4h
```

At or after that deadline, the evaluator reads only raw rows whose event time
is in the source interval and whose local receive time is no later than the
deadline. It sorts source identities and payload hashes before canonical JSON
serialization. Runtime clock time, database append order, replica path order,
run ID, and retry count are not evaluator inputs.

The precedence is fixed:

1. Any same-source-identity/different-payload conflict produces
   `FINAL_CONFLICT`.
2. Otherwise any absent, non-finite, schema-invalid, hash-invalid, or
   PIT-invalid required source produces `FINAL_MISSING`. The bounded-history
   periodic sources must also cover every expected interval slot: 48 rows for
   each 5-minute source and 240 completed rows for the 1-minute premium-index
   kline source. A source with only partial slot coverage is invalid, even when
   it has one or more valid rows.
3. Otherwise the result is `FINAL_COMPLETE`.

`finalized_at` is the logical deadline, while `recorded_at` preserves actual
write time. Re-running the evaluator checks the stored `evaluation_hash`; it
cannot insert or mutate another result. A row received after the deadline is
excluded by the fixed receive-time cutoff and appended as `LATE_ONLY`.

## Deadline-aware recovery

Normal polling/reconnect remains in place. In addition, the epoch supervisor
checks the just-closed interval every 30 seconds until its deadline. It retries
only provider endpoints that can faithfully return an event-time-bounded
history:

| source | bounded retry |
|---|---|
| `openInterestHist` | 5-minute rows with `startTime`/`endTime` |
| `basis` | 5-minute rows with `startTime`/`endTime` |
| `takerLongShortRatio` | 5-minute rows with `startTime`/`endTime` |
| `premiumIndexKline1m` | completed 1-minute rows with `startTime`/`endTime` |

Websocket PIT receive time, book/depth continuity, instantaneous OI, and live
premium/funding snapshots cannot be recreated honestly after the fact. The
supervisor therefore does not relabel a later snapshot as an earlier one.
Those sources rely on independent live replicas. Missing, invalid, and
partial-coverage recoverable sources are retried only while
`now < finalize_at`; the finalizer never waits past the deadline.
Retry eligibility is evaluated from that collector's local artifact only.
Finalization and `EPOCH_DEADLINE_RISK` remain based on the local+peer union.
This prevents one replica from hiding a recoverable hole in the other without
changing the immutable union verdict.

Each request, including invalid responses and transport/HTTP failures, appends
a terminal attempt row. The raw response body SHA-256 is retained even when
JSON/schema validation fails. Retry failures increment collector health failure
counts and emit an ERROR record with message and traceback. An unexpected
epoch/status supervisor exception is fail-stop: it is counted, logged with a
traceback, sets the collector stop event, and is re-raised.

## Two collectors and an independent watchdog

The operational topology is two collectors on distinct hosts/network paths,
each with its own local SQLite artifact and stable collector ID. They must not
share a writable SQLite file. Each process reads the other artifact through a
read-only replicated/mounted path; deterministic source-identity merge removes
byte-identical duplicates and exposes conflicting payloads.

A third process on an independent host watches both heartbeat/finalization
ledgers. It pages when fewer than two replicas are fresh or when a due epoch
has no matching finalization within the configured grace:

```bash
export R4_P0_WATCHDOG_ENABLED=true
export R4_P0_ALERT_WEBHOOK_URLS="https://alerts.example.invalid/r4"
uv run python -m scripts.r4_p0_watchdog --run \
  --artifact /path/to/host-a/r4_p0_collector.sqlite3 \
  --artifact /path/to/host-b/r4_p0_collector.sqlite3 \
  --minimum-healthy-replicas 2
```

At collector startup, `git rev-parse HEAD` is resolved from the loaded source
tree and written to `collector_process_versions` before collection tasks start.
The watchdog resolves its own deployed HEAD and compares it with the stamp tied
to each current heartbeat `run_id`. Missing stamps and hash mismatches are
`COLLECTOR_VERSION_MISMATCH`, remove that replica from the healthy count, and
fail the rehearsal version gate. A checkout without resolvable Git metadata
fails closed rather than starting an unstamped collector.

## Precommitted T0 procedure

T0 is not selected after startup verification. That would be circular: changing
the module constant creates a new commit and code hash, invalidating the
verification that was just performed. The operator sequence is fixed:

1. Commit `DEFAULT_T0`, `DEFAULT_STUDY_ID`, and `DEFAULT_POLICY_HASH` first,
   choosing a sufficiently distant UTC four-hour boundary.
2. Start both hosts from that exact fixed commit.
3. Before `T0-4h`, run the one-shot read-only preflight against both collector
   artifacts:

   ```bash
   uv run python -m scripts.r4_p0_watchdog --t0-preflight \
     --artifact /path/to/host-a/r4_p0_collector.sqlite3 \
     --artifact /path/to/host-b/r4_p0_collector.sqlite3
   ```

4. Proceed only when the JSON reports `ok: true`: verification time
   `V <= T0-4h`, every current process stamp matches the deployed code hash,
   at least two distinct replicas are healthy, and every stamp has the
   committed T0.
5. If any gate fails, do not retroactively adjust or reuse that T0. Precommit a
   new commit and a new T0, restart both hosts from that commit, and repeat the
   verification.

`--t0-preflight` does not require `R4_P0_WATCHDOG_ENABLED`; it opens collector
artifacts with SQLite `mode=ro`, performs one check, creates no watchdog state,
does no network I/O, and exits nonzero when a gate is closed.

Collector startup enforces the same contract before collection tasks or network
clients start. For the same `(study_id, policy_hash)`, a non-null stored
`t0_utc` that differs from the configured T0 is rejected. A completely fresh
artifact (`pit_records` has zero rows) is also rejected when startup is later
than `T0-4h`. An artifact with existing PIT rows is treated as a restart and is
not blocked by the warm-up check, while the stored-T0 check still applies.

The alert path is:

```text
epoch deadline risk (last hour) -> append-only alert ledger -> CRITICAL/WARNING log
                                                     \-> HTTPS webhook(s)
final MISSING/CONFLICT --------> DATA_INTEGRITY_FAIL through the same path
stale replica/finalizer --------> independent watchdog through the same path
missing/mismatched code stamp --> COLLECTOR_VERSION_MISMATCH through the same path
```

Logging is formatted in real UTC before adding the `Z` suffix. Webhook delivery
success/failure is appended separately, so an outage cannot erase its own
alert history.

No scheduler or service registration is part of this change. After manual
fault injection, operators may install the example `KeepAlive` launchd
templates in `ops/native/plists/examples/`; merely committing those templates
does not load or deploy them.

## Manual pre-arm verification

These checks write only temporary local artifacts and use public market data:

```bash
uv run pytest \
  tests/test_binance_r4_p0_collector.py \
  tests/test_binance_r4_p0_hardening.py -q

uv run python -m scripts.r4_p0_collector \
  --probe --duration 60 \
  --artifact-root /tmp/r4-p0-host-a

uv run python -m scripts.r4_p0_watchdog \
  --artifact /tmp/r4-p0-host-a/r4_p0_collector.sqlite3 \
  --artifact /tmp/r4-p0-host-b/r4_p0_collector.sqlite3
```

Before T_WEEK0, the operator-owned one-week rehearsal must use the same source
manifest, two-host topology, finalizer, and alert route. Acceptance is 100%
`FINAL_COMPLETE`, zero `FINAL_MISSING`, zero `FINAL_CONFLICT`, zero stalled
finalizers, a matching process-version stamp on both current collector run IDs,
and demonstrated page delivery under injected process/host loss.
