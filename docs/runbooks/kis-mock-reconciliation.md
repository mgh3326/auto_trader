# KIS Mock Holdings-Based Reconciliation Runbook (ROB-102)

## Purpose

`review.kis_mock_order_ledger` tracks KIS official-mock order lifecycle.
Because the KIS mock domestic pending-orders endpoint is unsupported and
cash/orderable APIs do not reliably indicate fills, this reconciler uses
**holdings deltas** (against a baseline captured at order-insert time) as
the primary fill signal.

Related: ROB-37 introduced the ledger. ROB-100 introduced the shared
lifecycle/account-mode contract. ROB-102 adds lifecycle tracking and the
holdings-delta reconciler.

---

## Lifecycle States (ROB-100 vocabulary)

| State | Meaning here |
|-------|--------------|
| `accepted` | Broker accepted the mock order; awaiting reconciliation |
| `pending` | No holdings delta yet; under stale threshold |
| `fill` | Holdings delta ≥ ordered qty (or partial delta) detected |
| `reconciled` | Post-fill holdings still match expected position |
| `stale` | No holdings delta after the stale threshold |
| `failed` | Broker rejected at submit time |
| `anomaly` | Holdings snapshot missing, baseline missing, or holdings disagree post-fill |

`reconciled`, `failed`, `stale` are terminal. `anomaly` is an operator
hand-off and is not terminal success/failure.

Fine-grained reasons (`reason_code`) live in `last_reconcile_detail`:
`fill_detected`, `partial_fill_detected`, `pending_unconfirmed`,
`stale_unconfirmed`, `position_reconciled`, `holdings_mismatch`,
`holdings_snapshot_missing`, `baseline_missing`.

---

## Baseline capture

When `_record_kis_mock_order` runs (immediately after broker acceptance),
it persists a snapshot of the **current KIS mock holdings qty** for the
symbol into `holdings_baseline_qty`. This is read-only against the
mock account (`fetch_my_stocks(is_mock=True)`).

If the broker call fails or a transient error occurs, `holdings_baseline_qty`
is left `NULL` and the reconciler will surface the row as
`baseline_missing → anomaly` for operator review.

---

## How to run

Default invocation (dry-run, returns proposals only):

```
kis_mock_reconciliation_run()
```

Apply transitions (operator-gated):

```
kis_mock_reconciliation_run(dry_run=False, confirm=True)
```

Tunable bound:

```
kis_mock_reconciliation_run(limit=100)
```

Thresholds (`pending_threshold_sec=60`, `stale_threshold_sec=1800`)
are currently configured at the reconciler default and can be tuned by
passing a `ReconcilerThresholds` to `run_kis_mock_reconciliation` directly
from a script or test.

---

## Safety

* No broker submit/cancel/modify calls.
* No KIS live-account access (`fetch_my_stocks(is_mock=True)` only).
* Direct SQL writes against `review.kis_mock_order_ledger` are forbidden;
  go through `KISMockLifecycleService`.
* `dry_run=False` requires `confirm=True` from the operator.
* No scheduler/launchd hooks added by ROB-102 — invoke manually for now.

## Troubleshooting

* `holdings_snapshot_missing` — KIS holdings call returned no row for the
  symbol; verify `fetch_my_stocks(is_mock=True)` and symbol normalization
  (KR `pdno`, US `ovrs_pdno`).
* `baseline_missing` — the order-insert baseline fetch failed at submission
  time. Either backfill via
  `KISMockLifecycleService.record_holdings_baseline` or hand off to the
  operator via the anomaly path.
* `holdings_mismatch` — post-fill holdings disagree with the expected
  position; do NOT auto-resolve. Escalate to operator.
