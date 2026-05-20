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

## 4. Confirmed submission (opt-in; actually hits testnet)

```bash
BINANCE_TESTNET_ENABLED=true \
BINANCE_TESTNET_API_KEY=$KEY \
BINANCE_TESTNET_API_SECRET=$SECRET \
uv run python -m scripts.binance_testnet_scalper_smoke \
  --duration 30 --no-dry-run --confirm
```

* `--confirm` must be passed on every invocation. It is **per-call**, not config-level — every submit-eligible tick must satisfy `confirm=True`.
* `--no-dry-run` is needed alongside; passing `--confirm` without `--no-dry-run` warns and stays dry-run.
* The runner is bounded to the locked MVP set (`BTCUSDT/ETHUSDT/SOLUSDT`) and to `max_notional_usdt = 10` unless the call-site supplies a `notional_override_reason`.

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

## 7. TP/SL representation (open item #6)

Spot doesn't have native OCO on testnet. The MVP records both legs as
two ledger rows linked by `parent_client_order_id`:

* Entry row: `client_order_id = E`, `parent_client_order_id = NULL`.
* TP row: `client_order_id = E-tp`, `parent_client_order_id = E`.
* SL row: `client_order_id = E-sl`, `parent_client_order_id = E`.

When either TP or SL triggers, the other is cancelled (`record_cancel`)
in the same transaction. The current MVP runner doesn't yet place
paired stop/limit orders — that's a follow-up; for now `_handle_exit`
issues a plain cancel.

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
