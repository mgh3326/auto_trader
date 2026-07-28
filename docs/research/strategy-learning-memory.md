# Strategy learning memory (ROB-1115)

`research.strategy_learning_events` is the append-only P0 memory for strategy
decisions and failures. It promotes the discipline in
`herdr-inbox/gptpro-krb1-packet/preregistration-discipline-2026-07-28.md` into a
typed database contract. The inbox document remains the source packet; this
document states the durable action rules.

## Identity and evidence

- `memory_event_id`, `request_hash`, `failure_fingerprint`, and
  `learning_payload` use the ROB-846 closed typed canonical AST authority.
  Finite floats use `float.hex()` and the persisted JSONB AST is never
  re-encoded before hashing.
- `reason_codes` retains the complete ordered array. Never collapse it to a
  primary reason.
- `evidence_refs` contains only SHA-256 references (optionally prefixed by a
  reference kind). Do not copy metric matrices, PnL series, or ledgers into the
  event.
- A repeated idempotency key returns the original row only when its
  `request_hash` is identical. Reusing the key for changed semantics fails
  closed.

## Registry FK while production is empty

`experiment_id` is a nullable FK to
`research.strategy_experiments.experiment_id`. PostgreSQL enforces the FK for
every non-null insert; with a zero-row parent table no non-null id can be
written. `NOT VALID` would not help because it still checks new rows.

The nullable state is therefore the explicit P0 bridge for existing R1–R4,
Alpaca, and KR-B1 tracks that have not been registered. New registered work
should always supply `experiment_id`; only those events participate in
`get_memory` and `get_lineage`. Unregistered events remain reusable through
`search_failures`. Retrospective registration/backfill and its
"not preregistered" provenance remain P1/operator decisions.

## Failure-to-action rules

- Crash or timeout is operational, not a strategy failure. Retry the same
  identity (`retry_same_identity`).
- Data-quality or point-in-time defects permit correction only on the data
  axis.
- With sufficient data, `no_signal` must not be rescued by post-hoc threshold
  relaxation. Change the mechanism or retire it.
- A failed gross edge must not be kept alive through small fee-model changes.
- Positive gross but negative net preserves the signal and changes exactly one
  of horizon, turnover, or execution.
- Robustness failure (DSR, PBO, or fold instability) reduces degrees of freedom.
  Do not add a post-hoc regime filter.
- If only MDD fails, change only sizing.
- Shadow-to-paper divergence investigates execution before changing the
  strategy.
- A sealed-OOS failure is never retuned against the sealed sample.

Corrections are new events. The service and database expose no UPDATE or DELETE
path; row UPDATE, DELETE, and table TRUNCATE are rejected by triggers.
