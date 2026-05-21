# Binance Testnet Scalping (ROB-286) — Runbook

**Scope.** Operator runbook for the testnet-only Binance Spot scalping
MVP introduced by ROB-286. Covers env vars, opt-in procedure,
default-disabled behavior, reconciliation, manual close, and the
production cutover gate.

**Hard invariant**: this adapter is *structurally* testnet-only. There
is no live mode. The class name (`BinanceTestnetExecutionClient`), host
allowlist (`TESTNET_HOSTS`), and transport factory all enforce this at
the type/runtime layer.

---

## 1. Env variables

| Variable | Required when opted in? | Default | Notes |
|---|---|---|---|
| `BINANCE_TESTNET_ENABLED` | Yes (must be `true`) | unset → disabled | Master kill-switch. Default behavior is fail-closed. |
| `BINANCE_TESTNET_API_KEY` | Yes | — | API key from `testnet.binance.vision`. |
| `BINANCE_TESTNET_API_SECRET` | Yes | — | API secret. Never logged. |
| `BINANCE_TESTNET_BASE_URL` | No | `https://testnet.binance.vision` | Validated against `TESTNET_HOSTS` at factory init; a live host (e.g. `api.binance.com`) raises `BinanceLiveHostBlocked`. |
| `BINANCE_TESTNET_MAX_NOTIONAL_USDT` | No | `10` | Per-order cap; override at call-site requires `notional_override_reason`. |

---

## 2. Default-disabled behavior

```bash
uv run python -m scripts.binance_testnet_scalper_smoke
# → exit 0; single log line "scalper disabled — set BINANCE_TESTNET_ENABLED=true to opt in"
# → zero HTTP, zero DB writes, zero Sentry events
```

This is the safe default. Production deployment of the adapter *without*
this env set leaves the scalper inert.

---

## 3. Opt-in (dry-run; still no HTTP submission)

```bash
BINANCE_TESTNET_ENABLED=true \
BINANCE_TESTNET_API_KEY=$KEY \
BINANCE_TESTNET_API_SECRET=$SECRET \
uv run python -m scripts.binance_testnet_scalper_smoke \
  --duration 30 --dry-run
```

Effect:
* `crypto_instruments` rows are read for `binance/spot/{BTCUSDT,ETHUSDT,SOLUSDT}` (run the seeder first; see §6).
* Reconciliation pass calls `open_orders` against testnet (signed GET).
  Per-symbol ledger drift is recorded as `anomaly` rows.
* Per tick, decision logic computes Hold/Entry/Exit.
* Entry decisions produce ledger trail `planned → previewed → validated`
  but **stop before `submitted`** because `dry_run=True` means
  `submit_order(confirm=False)` returns a `DryRunResult` and never
  performs the order POST.

---

## 4. Confirmed submission (opt-in; reaches testnet)

```bash
BINANCE_TESTNET_ENABLED=true \
BINANCE_TESTNET_API_KEY=$KEY \
BINANCE_TESTNET_API_SECRET=$SECRET \
uv run python -m scripts.binance_testnet_scalper_smoke \
  --duration 30 --no-dry-run --confirm
```

* `--confirm` must be passed on every invocation. It is **per-call**, not config-level — every submit-eligible tick must satisfy `confirm=True`.
* `--confirm` implies `--no-dry-run` at the CLI layer: when `--confirm` is set, the runner unconditionally executes with `dry_run=False`. There is **no warn-and-stay-dry-run path**. If the operator wants to keep dry-run while testing the `--confirm` argument plumbing, split it into two separate invocations.
* The runner is bounded to the locked MVP set (`BTCUSDT/ETHUSDT/SOLUSDT`) and to `max_notional_usdt = 10` unless the call-site supplies a `notional_override_reason`.

> **What this smoke does NOT auto-validate.** The smoke CLI's
> `_market_snapshot_stub` returns `rsi_5m=50.0` for every symbol, which
> always resolves to `Hold` in `decision.compute_action`. That means
> even with `--no-dry-run --confirm`, the runner will:
>
> * Execute the reconciliation pass (signed `GET /api/v3/openOrders`
>   reads — these DO reach testnet).
> * Run one tick per MVP symbol; each tick resolves to `Hold`.
> * Produce zero `submitted` / `filled` / `tp_sl_armed` ledger rows.
>
> Verifying the full `submitted → filled → tp_sl_armed → tp_sl_triggered
> → closed → reconciled` lifecycle therefore requires either:
>
> 1. wiring a real market snapshot (e.g., a Child B WS-derived snapshot
>    fed into `market_snapshot_for_symbol` — out of scope for the smoke
>    CLI), or
> 2. an operator-driven REPL invocation that calls `submit_order`
>    directly with a deterministic input, observes the fill, then lets
>    the runner place TP/SL.
>
> The smoke CLI's confirmed mode is intended to prove **connectivity +
> credentials + reconciliation read path + signed-endpoint plumbing**,
> not to exercise the full TP/SL lifecycle end-to-end on its own.

---

## 5. Reconciliation on startup (§B.C.10)

`ScalperRunner.reconcile_on_start` walks the MVP symbol set:

1. Fetches ledger rows in `submitted` / `filled` / `tp_sl_armed`
   (capped at `reconcile_open_orders_limit = 50`; rows older than
   `reconcile_lookback_hours = 24` are skipped with a
   `stamp_reconciliation_run` write).
2. Fetches `open_orders` from the broker (signed GET).
3. Each row whose `client_order_id` isn't in the broker's open-order
   set transitions to `anomaly` with `reason='reconcile_drift'`.

Anomaly rows fire a Sentry event (per open item #4 lean). Operators
must investigate and either manually clear the position or call
`record_reconciled` (anomaly → reconciled is the only post-anomaly
transition).

---

## 6. Instrument seeder

Before the smoke CLI can run a tick, `crypto_instruments` must have
rows for the MVP triplet:

```bash
uv run python -m scripts.binance_testnet_seed_instruments
# → idempotent; re-running is safe
```

`--dry-run` prints planned inserts without writing.

---

## 7. TP/SL representation — paired stop orders (ROB-289)

Spot doesn't have native OCO on testnet. ROB-289 wires real paired
stop orders at the testnet broker after an entry fill:

* Entry row: `client_order_id = E`, `parent_client_order_id = NULL`.
* TP row: `client_order_id = E-tp`, `parent_client_order_id = E`, broker
  type `STOP_LOSS_LIMIT` with `timeInForce=GTC`.
* SL row: `client_order_id = E-sl`, `parent_client_order_id = E`, broker
  type `STOP_LOSS` (stop-market, no `timeInForce`).

**Default path:** After the entry row transitions to `filled` the runner
places both legs SEQUENTIALLY (TP first, SL second — never via
`asyncio.gather`; parallel placement can produce ambiguous half-armed
state on a broker reject). Each leg walks the lifecycle:

    planned → previewed → validated → submitted → filled → tp_sl_armed

Once both legs are `tp_sl_armed`, the runner's `_derive_symbol_state`
treats the symbol as busy and the decision function holds until
either TP or SL triggers.

**Trigger behavior:** When the price snapshot crosses `tp_price` or
`sl_price`, the decision function returns `Exit(take_profit|stop_loss)`.
The runner:

1. Marks the triggered leg as `tp_sl_triggered`.
2. Looks up the sibling leg by the SHARED `parent_client_order_id`
   (never by the TP/SL CIDs themselves).
3. Cancels the sibling at the broker.
4. Records `cancelled(cancel_reason=opposite_leg_triggered)` on the
   sibling.

**Broker-reject fallback (§3 of the plan):** If `place_stop_limit_order`
or `place_stop_market_order` returns a 4xx:

* First-leg-success-then-second-leg-reject (the most dangerous path):
  the first leg is cancelled at the broker IMMEDIATELY before the call
  returns (avoids half-armed broker state), then `record_anomaly` is
  written on both the rejected leg and the entry row, then the existing
  cancel-and-close fallback runs.
* First-leg-reject: the second leg is skipped entirely (no retry), the
  rejected leg + entry row get `record_anomaly(reason=
  "tp_sl_placement_rejected")`, then cancel-and-close fallback.

**Unknown state (timeout / 5xx):** The in-flight leg is recorded with
`record_anomaly(reason="tp_sl_placement_unknown")` and reconciliation
on the next runner startup walks `open_orders` + `recent_fills` to
resolve the row deterministically. No auto-retry.

**Sibling cancel failure (§3.3):** If the sibling-leg cancel call
itself fails at the broker, the sibling row gets
`record_anomaly(reason="opposite_leg_cancel_failed")` — operator action
required; do not auto-retry.

**Operator boundary:** None of these placements happen without
`confirm=True` AND `dry_run=False`. The smoke CLI default keeps
`dry_run=True`, so paired TP/SL placement code is reachable only via
`--no-dry-run --confirm` (operator gated; ROB-293 is the live testnet
opt-in issue).

---

## 8. Manual close procedure

If the runner is in shadow mode or the operator needs to close a
position out-of-band:

1. Identify the entry's `client_order_id` from the ledger
   (`SELECT * FROM binance_testnet_order_ledger WHERE
   lifecycle_state IN ('submitted','filled','tp_sl_armed')`).
2. Cancel manually via testnet UI or REST.
3. Update the ledger with the appropriate transition
   (`BinanceTestnetLedgerService.record_cancel(...)` followed by
   `record_reconciled(...)`).

Never insert/update the ledger directly via SQL — the service layer
enforces the state machine and the audit trail.

---

## 9. Anomaly clear (operator-initiated)

```python
# inside a one-off async REPL or operator-only script
await service.record_reconciled(
    client_order_id=cid,
    extra_metadata={"cleared_by": operator, "reason": "investigated"},
)
```

`anomaly → reconciled` is the only transition from `anomaly` and
requires the operator's explicit intent.

---

## 10. Production cutover gate (deferred)

Same pattern as ROB-284 / ROB-285:

1. Pre-cutover DB backup of the target environment.
2. `uv run alembic upgrade head` against the **non-prod** server DB;
   verify `binance_testnet_order_ledger` exists with the CHECK
   constraint and is empty initially.
3. `uv run alembic downgrade -1 && uv run alembic upgrade head` round-trip.
4. Default-disabled smoke run: exits 0, single log line, zero side effects.
5. Opt-in dry-run smoke (30 s, `--dry-run`) produces
   `planned/previewed/validated` but zero `submitted` ledger rows.
6. Operator-initiated `--confirm` smoke against testnet (small notional,
   single symbol, 5-minute duration).
7. Production cutover is scheduled separately; this PR's merge alone
   does NOT enable any of the above.

---

## 10A. Evidence collection (ROB-293 smoke)

For each step of the ROB-293 operator smoke, record the following
artifacts. Treat anything missing as a stop-and-investigate signal —
do **not** advance to the next step on a partial pass.

**Step A — Instrument seeder (`scripts/binance_testnet_seed_instruments`)**

* Expected: idempotent insert of `binance/spot/{BTCUSDT, ETHUSDT,
  SOLUSDT}` rows; re-runs are no-ops.
* Evidence: stdout from `--dry-run` then the actual run; SELECT against
  `crypto_instruments WHERE venue='binance' AND product='spot'`
  returning exactly the three MVP rows.

**Step B — Default-disabled smoke (no env vars set)**

* Expected: exit code `0`; single log line `"scalper disabled — set
  BINANCE_TESTNET_ENABLED=true to opt in"`; zero HTTP, zero DB writes,
  zero Sentry events.
* Evidence: shell `$?`; captured stdout (one line); zero rows in
  `binance_testnet_order_ledger` afterwards.

**Step C — Opt-in dry-run smoke (`--dry-run`, env set)**

* Expected: `reconcile_on_start` runs (signed GET to testnet); per-tick
  decision returns `Hold` (stub snapshot); ledger gains zero
  `submitted` rows; zero `anomaly` rows.
* Evidence: stdout line `reconcile_on_start examined=N anomalies=0`;
  ledger query `SELECT lifecycle_state, count(*) FROM
  binance_testnet_order_ledger GROUP BY lifecycle_state` shows no
  `submitted`/`filled` rows added by this run; Sentry has zero new
  scalper events.

**Step D — Confirmed smoke (`--no-dry-run --confirm`, env set)**

* Expected (given §4 stub-snapshot limitation): `reconcile_on_start`
  performs a real signed GET against `testnet.binance.vision`; each
  tick still resolves to `Hold`; ledger again gains zero `submitted`
  rows.
* Evidence: stdout shows `reconcile_on_start examined=N anomalies=0`;
  testnet API request logs (if available) show signed GET to
  `/api/v3/openOrders`; no `BinanceLiveHostBlocked` raised; no secret
  string appears anywhere in captured logs (`grep -i $API_KEY` over
  the captured log is empty — the API key value itself must NOT be
  pasted into the report; just record the grep exit code).

**Step E (operator-driven, optional) — Full lifecycle**

* Expected: ledger walks `submitted → filled → tp_sl_armed (×2 legs) →
  tp_sl_triggered (one leg) → cancelled (sibling leg) → closed →
  reconciled`. Testnet UI shows the matching paired stop orders + their
  cancellation.
* Evidence: ledger transitions log (one row per `record_*` call), TP
  and SL order IDs from `StopOrderResult`, testnet UI screenshot
  (with API key fields blanked), Sentry event count = 0 anomaly + 1
  sanity `filled-after-submitted` (per open item #4 lean).

**General — what to attach to the ROB-293 closure comment**

* Step B exit code + one-line log.
* Step C ledger row delta + Sentry delta.
* Step D `reconcile_on_start` line + signed-GET evidence.
* Step E (if performed) ledger transition log + testnet UI screenshot.
* Explicit confirmation that `grep -F $BINANCE_TESTNET_API_KEY
  $BINANCE_TESTNET_API_SECRET <captured-log>` returned no matches.

Never paste raw credential values, raw signed query strings, or HMAC
signature hex into the closure comment.

---

## 10B. Smoke kill-switch + rollback (ROB-293)

If anything during the smoke goes off-script, follow this deterministic
sequence rather than improvising. Order matters.

**Immediate kill (any step)**

1. `Ctrl-C` the smoke CLI. (The runner has no in-process retry loop —
   one signal stops it cleanly.)
2. `unset BINANCE_TESTNET_ENABLED` in the operator shell. This restores
   the default-disabled gate so a stray re-invocation falls through to
   the exit-0 path.
3. Capture stdout + stderr into a file before exiting the shell.

**Triage by category**

| Symptom | Immediate action | Cleanup |
|---|---|---|
| `BinanceLiveHostBlocked` raised | Stop. Verify `BINANCE_TESTNET_BASE_URL` is the testnet host (or unset). Inspect `host_allowlist.py::TESTNET_HOSTS`. | None on ledger; failure is at adapter init before any write. |
| `BinanceMissingCredentials` raised | Stop. Re-export the env vars in the shell; do not echo the values. | None on ledger. |
| `BinanceTestnetDisabled` raised | Stop. Confirm `BINANCE_TESTNET_ENABLED=true` is truthy (`true`/`1`/`yes`/`on`). | None on ledger. |
| `reconcile_drift` anomaly fired | Stop the runner. Inspect the affected `client_order_id`s via testnet UI. Manually cancel any broker-side stragglers. Use `record_reconciled` (§9) to close out the anomaly row. | Ledger: anomaly → reconciled (operator-initiated). |
| `tp_sl_placement_rejected` or `tp_sl_placement_unknown` | Stop. Cancel any open broker-side stop orders via testnet UI. The plan's recovery is to let the next `reconcile_on_start` resolve, but during a smoke the operator clears manually to keep the audit trail clean. | Manual cancel + `record_cancel(..., reason="smoke_cleanup")` + `record_reconciled`. |
| Secret value appears in log output | Stop and treat as a credential exposure. Rotate the testnet API key immediately on Binance's testnet UI. File a follow-up issue; do NOT re-run smoke until the leak is patched. | Rotate first, then ledger cleanup if any rows were created. |
| Any other unexpected exception | Stop. Capture stdout + stderr. Treat as `not ready` and file an investigation note. | Inspect ledger for partial rows; clean via `record_cancel` + `record_reconciled` as needed. |

**Post-smoke verification (regardless of outcome)**

1. Query `binance_testnet_order_ledger` for rows added during the run
   window (`created_at >= <start>`). Confirm every row reached either
   `reconciled` or a deliberate intermediate stop (e.g., dry-run only
   reaches `validated`).
2. Confirm Sentry has zero unaddressed `anomaly` events for the run
   window. Each anomaly must have a paired `record_reconciled` audit
   row.
3. Confirm `binance_testnet_order_ledger` has no orphan
   `tp_sl_armed` rows whose sibling `client_order_id` (via
   `parent_client_order_id`) is not also in `tp_sl_armed` or a
   downstream state.

**Hard "do not" list during smoke**

* Do **not** run `alembic upgrade head` against a production DB —
  smoke targets a non-prod DB only.
* Do **not** enable any scheduler/TaskIQ/cron task — smoke is CLI-only
  invocation.
* Do **not** paste API key, API secret, signed query string, or HMAC
  signature anywhere outside the operator shell.
* Do **not** override `max_notional_usdt` above the default `10` for
  the smoke run.
* Do **not** broaden the MVP symbol set; smoke runs the locked triplet
  only.

---

## 11. What this PR does NOT do (locked non-goals)

Echoing the plan's forbidden scope:

* No live Binance trading (anywhere).
* No futures path (`testnet.binancefuture.com` is NOT in
  `TESTNET_HOSTS`).
* No `reduceOnly` parameter on spot signatures.
* No scheduler/TaskIQ/cron activation. CLI-only invocation; audit test
  enforces.
* No production deploy.
* No real-money mutation through any code path.

---

## 12. Test surface (matrix anchors)

| Row | Test | What it locks |
|---|---|---|
| T1 | `test_testnet_and_public_hosts_are_disjoint` | TESTNET_HOSTS ∩ PUBLIC_HOSTS = ∅ |
| T9 | `test_signed_request_to_public_host_raises` | Cross-allowlist guard fires |
| T10 | `test_disabled_by_default_raises_on_construct` | Default fail-closed |
| T11/T12 | missing-credential tests | Fail-closed on missing key/secret |
| T17 | `test_sign_request_params_canonical` | HMAC chokepoint pinned |
| T31 | `test_smoke_disabled_by_default_no_side_effects` | Smoke CLI default-disabled |
| T32 | `test_smoke_dryrun_creates_no_submitted_rows` | Operator gate |
| T33 | `test_no_live_host_url_in_testnet_package` | No `api.binance.com` literal |
| T34 | `test_no_scheduler_activation` | No scheduler drift |
| T35 | `test_no_signed_endpoint_surface_in_binance_public_package` | Child B public adapter unchanged |
