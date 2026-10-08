# kis_mock rows 80/66/64/63 — Q-46 expired[inference] close (#1250)

Operator decision hk #706 comment 1092 (2026-10-05 06:1x KST): apply the #1112
`expired[inference]` rule to kis_mock ledger rows 80, 66, 64 and 63,
`operator_decision_ref` Q-46, strategy match skipped, exactly these four ids.
After the close, H3-KR can start on kis_mock.

Why a separate lever: the #881 tool `kis_mock_ledger_expire_day_orders` needs a
recorded zero fill, which July/August kis_mock rows cannot have (KIS mock has no
pending inquiry and daily-ccld keeps D+1 only). The 10-01 desk dry run refused
80/66/63 as `fill_unknown` and 64 as `expected_strategy_invalid`
(hk doc `ops/2026-10-01/706-phase-b-desk-run`).

Running it against a real database is the operator's step. Builders and testers
only ran it against throwaway databases.

## 1. What the tool does

`scripts/expire_kis_mock_rows_by_inference.py`

- Preview by default: prints every row's per-condition verdict, writes nothing.
- `--commit`: locks the four rows (`FOR UPDATE`), re-checks every condition
  under the lock, and closes all four in one transaction, or changes nothing.
- `--ids` must be exactly 80, 66, 64, 63 (order free). Any other id, a subset,
  a duplicate, a range or a sign exits 1 before a database connection exists.
- `--decision-ref` must be exactly `Q-46`. `--reason` and `--actor` are required.
- A second `--commit` after a successful one is a no-op (exit 0, `status: noop`,
  `changed: 0`; no UPDATE, no audit row, no attempt-counter change).
- No delete path. No broker call. No live ledger is read.
- Ids are accepted only as exactly the four built-in ints (API callers too):
  `63.0`, `numpy.int64(63)`, `True` and `"63"` are refused before any SQL.
- Inside the one commit transaction: lock + classify; the write chokepoint
  (`KISMockLifecycleService.close_rows_by_q46_inference`) re-locks and
  re-classifies the batch itself and accepts only this rule's marker-shaped
  details, so a direct call cannot close a refused row; guarded UPDATEs (exactly
  four, else it raises); **every fill source is re-read after the UPDATEs** and
  the batch re-classified on the pre-update facts (a fill committed between the
  evidence read and the UPDATE refuses the batch with full rollback); four
  audit rows; COMMIT.
- A closed row is terminal in the DB: a BEFORE UPDATE OR DELETE trigger
  refuses any change or removal of a row carrying the marker, so clearing the
  marker cannot reopen it (r3).
- The DB couples close and audit: an audit INSERT is refused unless its ledger
  row is already `expired` with this marker, Q-46 and the same batch id; a
  deferred constraint trigger refuses at COMMIT any row carrying the marker
  that is outside the four ids, not an accepted/pending -> expired transition,
  or has no same-batch audit row; at COMMIT it also requires exactly four
  audit rows in the batch and re-checks every fill source (see section 5).

Exit codes: 0 eligible preview / committed / no-op, 1 input or database error,
2 refused batch.

## 2. Conditions (every one must hold for every row, or the batch is refused)

The #1112 rule translated to kis_mock evidence. Two #1112 conditions are waived
by the Q-46 decision and nothing else: `strategy_match` and `reconcile_coverage`
(section 6). The preview prints `waived_conditions` on every row, so the
operator sees exactly what is waived before `--commit`; the commit is the
operator's confirmation. The same list is written to every closed row and audit
row next to the `no_broker_original` caveat.

| # | condition | kis_mock evidence |
|---|---|---|
| 1 | `kis_mock_accepted_buy_row` | kis_mock / kis / equity_kr / KRW BUY, `status=accepted`, not a scalping row, 6-digit symbol, numeric order number, stored response `rt_cd=0`, `odno` = order number, `ord_tmd` = order time |
| 2 | `kis_mock_row_open` | lifecycle still `accepted` or `pending` |
| 3 | `day_order` | limit with price > 0 (ORD_DVSN 00) or market with price 0 (01); whole positive quantity |
| 4 | `regular_session_accept` | recorded send instant and broker `ord_tmd` both inside the XKRX regular session of a confirmed trading day; ROB-671 says `regular` |
| 5 | `day_close_passed` | now > the #1112 deadline (latest of submit-day 15:30 KST, calendar close, ROB-671 conservative expiry = 20:00 KST for a regular-session buy) |
| 6 | `no_fill_recorded_for_order` | no `review.execution_ledger` kis/mock row for the order number (any source, quarantined rows included); no fill reason code / non-zero or unreadable `attributed_fill_qty` on the row; no same-correlation kis_mock row with fill evidence |
| 7 | `holding_quantity_unchanged` | `holdings_baseline_qty` recorded at send (known holding) and no recorded fill of the symbol that may postdate the accept instant (kis_mock rows with fill evidence unless placed and reconciled before accept; execution_ledger kis/mock rows filled at/after accept) |

Fill evidence reason codes (kis_mock holdings reconciler): `fill_detected`,
`partial_fill_detected`, `position_reconciled`, `holdings_mismatch`,
`attribution_unconfirmed`.

#1112 condition `execution_ledger_covers_order_day` (a committed reconcile run
covering the order day): see section 6.

## 3. What a closed row looks like

- `lifecycle_state = expired`, `reconcile_attempts + 1`, `reconciled_at` = close time.
- `last_reconcile_detail`:
  - `reason_code` = `expired_inference:kis_regular_day_order_no_broker_original`
    (the exact #1112 marker; `is_expired_inference_reason` is true)
  - `expiry_basis` = `inference`, `expiry_caveat` = `no_broker_original`
  - `operator_decision_ref` = `Q-46`, `inference_rule`, `rule_version`,
    `waived_conditions`, accept instant, deadline and its reason, reason, actor,
    batch id, and the prior detail.
- Because the row is terminal it drops out of every open-order reader exactly
  like any expired row: `KISMockLifecycleService.list_open_orders` (shadow
  pending list, shadow cash/sellable reservation, kis_mock reconcile scope);
  the generic transition API refuses it (`ExpiredLifecycleConflict`); the #881
  tool reports `already_terminal`.
- One row per ledger row in `review.kis_mock_inference_expiry_events`
  (append-only; UPDATE/DELETE/TRUNCATE rejected by trigger; CHECKs: ledger_id in
  63/64/66/80, action `expire_inference`, decision ref `Q-46`, before state
  accepted/pending, after state expired; `ledger_id` UNIQUE). `evidence` holds
  the full per-row verdict, the before snapshot and the written detail.

## 4. Operator procedure

Prerequisite: migration `20261005_t1250_kismock_inf` applied
(`uv run alembic upgrade head`, operator step; CREATE TABLE only). Under the
#789 Stage 4 split the migration grants `at_app` SELECT, INSERT on the audit
table and its sequence; the CLI also needs the existing UPDATE privilege on
`review.kis_mock_order_ledger` (row lock + guarded UPDATE). The migration also
adds a BEFORE INSERT trigger on the audit table and a deferred constraint
trigger on `review.kis_mock_order_ledger` (fires only for rows carrying this
rule's marker; CREATE TRIGGER takes a brief SHARE ROW EXCLUSIVE lock — apply
outside KRX hours).

Name the database through one environment variable that already holds the URL
(the value is never printed); replace `AT_DB_URL_ENV_NAME` with that variable's
name.

Preview (writes nothing):

    uv run python -m scripts.expire_kis_mock_rows_by_inference \
      --database-url-env AT_DB_URL_ENV_NAME \
      --ids 80,66,64,63 --decision-ref Q-46 \
      --reason "hk 706 c1092: stale Jul-Aug shadow pending buys, #1112 inference, strategy match skipped" \
      --actor operator-desk

Read every row: `verdict`, `failed_conditions`, `accept_at`,
`inferred_expiry_after`. Commit only on `status: eligible` (exit 0).

Commit (same arguments plus `--commit`):

    uv run python -m scripts.expire_kis_mock_rows_by_inference \
      --database-url-env AT_DB_URL_ENV_NAME \
      --ids 80,66,64,63 --decision-ref Q-46 \
      --reason "hk 706 c1092: stale Jul-Aug shadow pending buys, #1112 inference, strategy match skipped" \
      --actor operator-desk --commit

Expect `status: committed`, `changed: 4`, a `batch_id`. A repeat prints
`status: noop`.

Read back (SELECT only):

    SELECT id, lifecycle_state, last_reconcile_detail->>'reason_code',
           last_reconcile_detail->>'operator_decision_ref', reconciled_at
      FROM review.kis_mock_order_ledger WHERE id IN (63, 64, 66, 80) ORDER BY id;
    SELECT ledger_id, batch_id, before_state, after_state, actor, created_at
      FROM review.kis_mock_inference_expiry_events ORDER BY ledger_id;

If the preview refuses: do not edit rows by hand. The refusal names the
condition per row; a refusal is the rule working (for example a later fill of
the same symbol in the kis_mock ledger). Report it; a different close needs a
new operator decision.

## 5. Residual risk

- Fill timing (r3): a fill committed after the service's post-UPDATE re-read
  is caught by the deferred COMMIT-time trigger, which re-runs the order,
  symbol (at/after accept) and same-correlation / same-symbol kis_mock fill
  checks in SQL while COMMIT is processed and refuses the whole close (the CLI
  then reports `refused`). What remains is a fill that commits during this
  transaction's own COMMIT processing, after the trigger's statements — two
  transactions committing at the same instant. The row then stays expired with
  the inference marker and the fill is still recorded in `execution_ledger`;
  the marker makes the inference auditable (#1112 accepts the same). The SQL
  fill helper `review.kis_mock_q46_detail_has_fill` mirrors the Python rule
  (fill reason codes pinned by a test).

- No broker read is made: a fill KIS mock recorded but this repository never
  ledgered would not be seen. That is exactly the `no_broker_original` caveat
  the decision accepts; the holding check at least requires that no ledgered
  fill of the symbol may postdate the accept instant.
- `holdings_baseline_qty` is the only kis_mock "known holding" fact; a row
  without it is refused.
- The ROB-671 deadline treats the kis_mock order as a possible SOR carry
  (20:00 KST); that is the conservative choice and irrelevant for months-old
  rows.

## 6. #1112 reconcile-coverage condition — waived (director-1 option A)

#1112 `execution_ledger_covers_order_day` requires a committed KIS
execution-ledger reconcile run covering accept..deadline. For kis_mock no such
run can exist: `review.execution_ledger_reconcile_runs` has no account mode and
the reconciler reads only the live account, and the kis_mock holdings
reconciler never touched rows 80/66/64/63 (still `accepted`, never `pending`).
The 10-05 decision exists precisely because that evidence cannot exist for
July/August kis_mock rows (strategy-lab, hk #706 comment 1053). Director-1
ruled option A on hk task 1250: the condition is waived under the same Q-46
decision, recorded per row as `waived_conditions = [strategy_match,
reconcile_coverage]` with the `no_broker_original` caveat. The rule, service and
CLI never read reconcile runs (a test pins this). The waiver exists only inside
the hard four-id allowlist; every other condition stays strict.

## 7. Migration proof (builder, throwaway TimescaleDB PostgreSQL 17)

Fresh database, `create extension timescaledb`, `alembic upgrade head`
(whole chain) → `downgrade -1` → `upgrade head`: both head schema dumps of
`review` identical; the downgraded dump identical to a separate database
upgraded only to `20261001_t1175_ledger_quar` (only pg_dump's per-run
`\restrict` token differs). Re-run after the r2 triggers with the same result,
plus an end-to-end CLI preview (eligible) -> `--commit` (committed, 4) ->
`--commit` (noop) against the alembic-migrated database.
