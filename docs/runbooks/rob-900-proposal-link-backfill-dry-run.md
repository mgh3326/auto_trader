# ROB-900 Proposal-Link Backfill Dry Run

## Purpose

List conservative, operator-reviewable suggestions for terminal Toss ledger rows
that have no exact `correlation_id` or `broker_order_id` link to an order
proposal rung. This is an evidence-gathering tool only; it cannot backfill,
reconcile, submit, cancel, or call a broker.

## Preconditions

- Use a production environment only after loading its normal application
  configuration. Do not paste credentials into shell history or output.
- Confirm the requested scope is proposal-link evidence review, not a database
  mutation. There is deliberately no `--apply`, `--commit`, or write mode.
- Do not run this as a substitute for `toss_reconcile_orders`; linked rows are
  repaired by the existing reconcile path.

## Run

```bash
uv run python scripts/list_rob900_toss_projection_backfill_candidates.py \
  --limit 500 --window-seconds 300
```

The command starts its database transaction with `SET TRANSACTION READ ONLY`
and emits one JSON document. Save its output as review evidence rather than
editing it into a migration.

## Matching and guards

A suggestion is emitted only when exactly one live Toss proposal rung matches:

- the same symbol, normalized market, and side;
- the exact requested/filled quantity; and
- a rung `updated_at` within the requested window of ledger `created_at`
  (default: 300 seconds).

`broker_order_id` is displayed so an operator can independently inspect the
broker evidence, but missing proposal links are never inferred from a partial
identifier. Zero candidates, more than one candidate, quantity mismatches,
market/side mismatches, and timestamps outside the window are excluded. Every
emitted row has `auto_backfill_eligible: false`.

## Review and next step

For each suggestion, an operator must independently verify broker order detail,
proposal approval/submission evidence, symbol/side/quantity, and timestamps.
This PR intentionally provides no execution mechanism. Any approved database
backfill must be designed, reviewed, and run as a separate authorized change
with its own audit trail and rollback plan.
