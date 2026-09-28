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
| `expired` | Operator Q-46 legacy DAY classification, with bounded row-local audit |

`reconciled`, `failed`, `stale`, `cancelled`, and `expired` are terminal. `anomaly` is an operator
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

## #706 legacy DAY terminal review (Task 881)

The hermes-paper-kis profile alone registers
`kis_mock_ledger_expire_day_orders`. This is an operator-invoked ledger
classification; it does not construct a KIS client, read the broker, send an
order, or run on a schedule. It requires KIS_MOCK_ENABLED and the existing
required mock configuration even though this operation is DB-only. The
configured minimum age is KIS_MOCK_TERMINAL_MIN_SESSIONS, default 2.

First inspect the explicit candidate IDs with
`kis_mock_ledger_expire_day_orders(ledger_ids=[...], operator_decision_ref="hk #706 Q-46", expected_strategy="actual_strategy")`.
The default dry_run is true. After reviewing every row's before status,
row-local evidence, rule version, decision, and after status, the operator may
invoke the same IDs and reference with `dry_run=False, confirm=True`. This
runbook does not authorize that execution; the operator does it after the
specified #180 freeze and #706 window.

The rule requires a KR cash equity row in the kis_mock account, the expected
strategy and correlation ID, a native accepted response with exact order number
and time, and regular domestic ORD_DVSN 00 (positive-price limit) or 01
(zero-price market). The native route is
`app/mcp_server/tooling/order_execution.py` →
`app/mcp_server/tooling/kis_mock_ledger.py::_record_kis_mock_order` →
`_save_kis_mock_order_ledger`; the broker cash-order request mapping is in
`app/services/brokers/kis/domestic_orders.py::order_korea_stock`.
`app/services/brokers/kis/mock_scalping_exec/adapters.py` also calls the save
helper, but its scalping role and absent accepted response cause refusal.
Mirror or report-item reservation fields also cause refusal.
Any row whose writing path is unknown or cannot be positively tied to the
native order-cash response is refused; order_type alone never proves DAY.

The row must carry pending_unconfirmed with attributed_fill_qty exactly zero,
and neither the local KIS mock lifecycle nor the execution ledger may contain
a matching fill row. The XKRX calendar must classify the trade date and at
least the configured number of completed trading sessions before today.
The count excludes both the order's own session and today: it includes only
open XKRX sessions strictly after the trade date and strictly before today.
For example, at the 2026-09-28 cutoff with N=2, 2026-09-21 has two completed
sessions (September 22 and 23) and is eligible by age, while September 22 has
one and is too recent; the intervening Chuseok closure adds no sessions.
Unknown calendar dates, missing or inconsistent order terms, unknown fill,
partial or filled rows, too-recent rows, and already-terminal rows remain
unchanged with explicit refusal codes. A confirmed eligible row becomes the
distinct expired lifecycle state through KISMockLifecycleService. Its bounded
audit detail retains the operator decision reference and rule version; a
second invocation does not change the row or audit counters.
The generic lifecycle transition method rejects a request to enter expired
and rejects any later transition out of an expired row. Only the locked,
rechecked legacy DAY method can write this state.

This classification uses persisted local facts and the calendar only. The KIS
mock pending inquiry is unsupported, so no broker-open-order proof is claimed.
The residual risk is an unrecorded broker fill or open order; the operator
must inspect the dry-run evidence and retain the Q-46 decision reference.
