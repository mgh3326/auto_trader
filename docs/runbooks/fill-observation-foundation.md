# Fill observation foundation (ROB-1195)

## Purpose and boundary

`review.fill_observations` is the immutable authority for a positive fill delta
that was proven by broker evidence. The invariant is:

> A broker-evidenced fill delta is durably appended once. Re-observing the same
> broker fill sequence or cumulative quantity appends zero delta.

ROB-1195 only ships the schema and service-layer foundation. It does **not**
wire KIS, Upbit, Toss, Alpaca, Binance, or Kiwoom reconcile paths to the writer.
It does not change `review.trades`, `review.execution_ledger`, any broker-native
ledger, trade journals, proposals, scheduler registration, or broker gates.

The three additive tables are:

- `review.fill_observations`: append-only broker evidence and positive deltas.
- `review.fill_projection_outbox`: durable pending/processing/retry/succeeded
  delivery state. `(projection_name, fill_observation_id)` is unique.
- `review.fill_projection_cursors`: durable high-watermark per
  `(projection_name, partition_key)`.

The observation migration installs database triggers that reject UPDATE,
DELETE, and TRUNCATE. A downgrade also refuses to remove the schema once an
observation exists.

## Identity and delta rules

The service canonicalizes a deterministic SHA-256 identity from:

1. broker, account reference, account mode, venue, and broker order ID; plus
2. broker fill sequence when available, otherwise canonical cumulative fill
   quantity.

The writer obtains a transaction-scoped PostgreSQL advisory lock derived from
the canonical order scope using a stable signed 64-bit SHA-256 prefix. It then
checks the deterministic identity before calculating a delta.

- Same identity and same semantic evidence hash: duplicate, zero write.
- Same identity and different semantic evidence hash: fail closed, zero write.
- Larger cumulative quantity: append only `cumulative - already recorded`.
- Equal cumulative quantity: zero delta, zero write.
- Regressed cumulative quantity: fail closed, zero write.
- Sequence-only evidence: append the positive reported fill quantity once.
- Missing evidence reference, missing sequence/cumulative identity, negative
  quantity, or zero total filled quantity: zero observation.

`evidence_ref` must point to retained sanitized broker/native-ledger evidence.
Do not put credentials, tokens, or an unredacted broker response in it.

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

For a new positive delta, `FillObservationWriter` inserts the observation and
its `legacy_dual_read_validation.v1` outbox row in one database transaction.
Neither row commits alone.

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
3. any legacy mismatch is explained and retained as rollout evidence; and
4. no observation exists for an order without a broker evidence reference.

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

The migration file is additive, but applying it is operator-owned. ROB-1195
development and verification must not run `alembic upgrade`, touch a shared
database, or perform a live backfill. Static Alembic head inspection and
unit/contract tests are the permitted pre-PR validation path.
