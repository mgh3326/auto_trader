# Fill observation foundation (ROB-1195)

## Purpose and boundary

`review.fill_observations` is the immutable authority for a positive fill delta
that was proven by broker evidence. The invariant is:

> A broker-evidenced fill delta is durably appended once. Re-observing the same
> broker fill fact appends zero delta, whether or not the provider has since
> revised the settlement values of that fill.

Only a contradicted **fill fact** fails closed. Post-trade revisions — fee,
average or last price, notional, settled `filled_at` — are not contradictions;
they are appended to a separate settlement history. See "Fill fact versus
settlement enrichment" below for the exact split.

ROB-1195 only ships the schema and service-layer foundation. It does **not**
wire KIS, Upbit, Toss, Alpaca, Binance, or Kiwoom reconcile paths to the writer.
It does not change `review.trades`, `review.execution_ledger`, any broker-native
ledger, trade journals, proposals, scheduler registration, or broker gates.

The four additive tables are:

- `review.fill_observations`: append-only broker evidence and positive deltas.
- `review.fill_settlement_enrichments`: append-only revision history of the
  post-trade values for one observation. `(fill_observation_id, revision)` is
  unique and `revision` is dense and 1-based.
- `review.fill_projection_outbox`: durable pending/processing/retry/succeeded
  delivery state. `(projection_name, fill_observation_id)` is unique.
- `review.fill_projection_cursors`: durable high-watermark per
  `(projection_name, partition_key)`.

The migration installs database triggers that reject UPDATE, DELETE, and
TRUNCATE on both `fill_observations` and `fill_settlement_enrichments`. A
downgrade also refuses to remove the schema once an observation exists.

## Identity and delta rules

The service canonicalizes a deterministic SHA-256 identity from:

1. broker, account reference, account mode, venue, and broker order ID; plus
2. broker fill sequence when available, otherwise canonical cumulative fill
   quantity.

US equity symbols are canonicalized to dot-separated upper case (`brk-b` and
`BRK/B` both become `BRK.B`) through `app/core/symbol.py`. Case and separator
drift between two polls of the same order is representation, not a different
fill. Crypto, KR, forex, and index symbols are never rewritten.

The writer obtains a transaction-scoped PostgreSQL advisory lock derived from
the canonical order scope using a stable signed 64-bit SHA-256 prefix. It then
checks the deterministic identity before calculating a delta.

- Same identity and same fill fact: duplicate, zero fill delta. Settlement is
  reconciled per the next section.
- Same identity and a **contradicted fill fact**: fail closed, zero write.
- Larger cumulative quantity: append only `cumulative - already recorded`.
- Equal cumulative quantity: zero delta, zero write.
- Regressed cumulative quantity: fail closed, zero write.
- Sequence-only evidence: append the positive reported fill quantity once.
- Missing evidence reference, missing sequence/cumulative identity, negative
  quantity, or zero total filled quantity: zero observation.

`evidence_ref` must point to retained sanitized broker/native-ledger evidence.
Do not put credentials, tokens, or an unredacted broker response in it.

## Fill fact versus settlement enrichment

`fill_observations.fill_fact_hash` fingerprints only the stable broker fill
fact: the order scope, `instrument_type`, `symbol`, `side`, `currency`, and
exactly the quantity the identity is keyed on. Under sequence identity that is
the sequence plus its own reported `fill_quantity`; under cumulative identity it
is the cumulative quantity. A mismatch there is a genuine contradiction and
raises `FillObservationIdentityConflict` with zero write.

Everything a provider legitimately revises afterwards is carried in
`review.fill_settlement_enrichments` and is deliberately **outside**
`fill_fact_hash`:

- `fee_total`, `average_price`, `last_fill_price`, `cumulative_notional`, and
  the settled `filled_at`;
- the quantity field the identity is *not* keyed on. Under sequence identity the
  order's cumulative quantity legitimately grows while the same fill is
  re-observed; under cumulative identity the per-poll reported increment is a
  snapshot of that poll, not a property of the cumulative state.

`evidence_source`, `evidence_ref`, and `observed_at` are the provenance of a
poll, not settlement values, so they stay out of `settlement_hash`. A repeated
poll of unchanged settlement is therefore idempotent instead of appending one
revision per poll.

Reconciliation rules, all under the order-scope advisory lock:

- No settlement value supplied at all: no revision, status `absent`.
- The values equal the **latest** revision: no revision, status `unchanged`, and
  the write result echoes that revision number.
- The values differ from the latest revision: append `revision + 1`, status
  `recorded`. A reverted correction appends a further revision rather than
  rewriting history, so the highest revision is always the most recently
  observed settlement.
- No observation row exists for the identity (`no_delta`, `no_fill_evidence`, or
  `writer_disabled`): status `not_applicable`.

The observation's own economic columns hold the settlement values **as first
observed** and never change; revision 1 is that same snapshot. Consumers that
need the current settlement must read the highest revision, not the observation
row. Nothing in this path issues an UPDATE against `fill_observations`, and a
settlement revision never produces a fill delta or an outbox row.

## Default-off activation

The only new flag is:

```text
FILL_OBSERVATION_WRITER_ENABLED=false
```

Absence, an empty value, and unknown values are all treated as false. When the
writer is disabled, it does not open a database session. This PR deliberately
has no runtime caller, so deployment of the schema alone writes nothing.

Setting the flag true does not create a caller. Wiring a broker reconcile path
to `FillObservationWriter` is a later consumer migration and requires separate
approval. That migration must keep the existing broker evidence gate and must
not start mid-order unless its cumulative baseline behavior has been reviewed.
Live backfill is outside this runbook.

## Atomic outbox and cursor behavior

For a new positive delta, `FillObservationWriter` inserts the observation, its
`legacy_dual_read_validation.v1` outbox row, and settlement revision 1 in one
database transaction. No row commits alone.

`FillProjectionQueue` is an unscheduled service foundation:

- `claim` leases ready or expired deliveries with `FOR UPDATE SKIP LOCKED` and
  increments the durable attempt count. It only leases the oldest unfinished
  observation in each partition, so a cursor cannot skip an earlier delivery.
- `retry` clears the lease and persists `last_error` plus `available_at` in one
  transaction.
- `complete` takes a deterministic projection-partition advisory lock, marks
  the outbox row succeeded, and advances the cursor in one transaction. A
  delivery older than the durable cursor fails closed.

No TaskIQ, cron, Prefect, CLI, MCP, or HTTP consumer is registered by ROB-1195.

## Dual-read validation

`FillObservationDualReader.validate_order(...)` is read-only. It compares the
sum of immutable observation deltas with both available legacy projections:

- `review.trades.quantity` for `(account, order_id)`; and
- `review.execution_ledger.filled_qty` for
  `(broker, account_mode, venue, broker_order_id)`.

Its statuses are `match`, `mismatch`, `new_only`, `legacy_only`, and `empty`.
`mismatched_sources` identifies which present projection differs. This makes
the existing `review.trades` `(account, order_id)` conflict visible when a
later partial-fill delta was discarded there; it does not repair or mutate the
legacy row.

Before a future consumer cutover, validate a bounded set of newly dual-written
orders and require:

1. repeated cumulative observations return zero delta;
2. observation quantity equals broker cumulative evidence;
3. any legacy mismatch is explained and retained as rollout evidence;
4. no observation exists for an order without a broker evidence reference; and
5. late fee/price settlement produced settlement revisions rather than either a
   second fill delta or a fail-closed conflict.

## Local verification

`tests/services/fill_observation/test_postgresql_integration.py` exercises the
real SQL, constraints, and triggers against the isolated pytest-owned local
`test_db` database (`tests/_run_owned_database.py` refuses any base URL whose
database is not `test_db`). It covers observation+outbox atomicity, rollback on
failure, the append-only triggers, partition ordering with
`FOR UPDATE SKIP LOCKED`, lease fencing, cursor advance/regression, settlement
revisioning, and an upgrade/downgrade/upgrade round trip on a throwaway local
database.

Run it with `uv run pytest tests/services/fill_observation/ -q`. It never
contacts a broker, an external API, a shared database, or production. Because
observations are append-only the tests cannot delete their rows, so each one
scopes itself to a fresh UUID order scope.

## Writer-off rollback

Rollback is additive and non-destructive:

1. Set `FILL_OBSERVATION_WRITER_ENABLED=false` (or remove it) in the process
   environment and restart only the separately approved caller process.
2. Confirm the caller reports `writer_disabled` and that no new
   `fill_observations.created_at` values appear after the restart.
3. Continue the unchanged native reconcile/ledger paths.
4. Leave existing observations, outbox rows, and cursors intact for audit and
   replay. Do not DELETE, TRUNCATE, or force an Alembic downgrade.

Writer-off does not authorize broker calls, backfill, projection repair,
consumer cutover, or mutation of any legacy ledger.

## Migration ownership

The migration file is additive, but applying it to any shared or production
database is operator-owned. ROB-1195 development and verification must not touch
a shared or production database and must not perform a live backfill.

The permitted pre-PR validation path is static Alembic head inspection,
unit/contract tests, and the isolated local integration suite above — including
its upgrade/downgrade/upgrade round trip, which runs against a throwaway
database created and dropped on the local test PostgreSQL instance. No other
`alembic upgrade` is in scope for this issue.
