# Binance Demo Scalping — Observe-Only Signal Runner (ROB-307 PR1)

PR1 of the Binance Demo scalping MVP: a **read-only, observe-only**
deterministic signal runner plus the risk/order-intent contract and
ledger-backed durable state. **It never places, previews, or tests an
order.** Later PRs add execution (PR2), TP/SL (PR3), and the default-OFF
scheduler scaffold (PR4).

> **⚠️ ROB-316 finding (2026-05-26) — the scalping signal here has no validated backtest edge.**
>
> ROB-316 backtested the deterministic trend micro-breakout signal (`demo_scalping/signal.py`) on ~60 days of XRPUSDT tick data (NautilusTrader): **net-negative at realistic fees and gross-negative out-of-sample** (232 trades); a 14-day apparent edge was overfit. See [ROB-316](https://linear.app/mgh3326/issue/ROB-316) and `docs/plans/ROB-316-*`.
>
> **Posture:** the whole demo scalping pipeline — the observe-only runner, execution (PR2+), the **5-min Prefect tick**, and the **WS daemon** (`binance-demo-ws-scalping.md`) — is **plumbing / harness for a future validated signal**, not a strategy-alpha runner. Keep the signal **disabled or observe/dry-run only**; `confirm=true` and recurring activation require a **separate validated-signal gate**.

## What PR1 ships

| Module | Responsibility |
|---|---|
| `app/services/brokers/binance/demo_scalping/contract.py` | Risk envelope (§5) + `evaluate_risk` + reason codes |
| `…/signal.py` | Deterministic trend micro-breakout signal |
| `…/market_data.py` | Read-only Demo-host klines/bookTicker adapter (own fail-closed allowlist) |
| `…/ledger_state.py` | Durable `LedgerSnapshot` from `binance_demo_order_ledger` |
| `…/runner.py` | Orchestration → `ObserveOnlyRecord` |
| `scripts/binance_demo_scalping_signal.py` | Observe-only CLI (default-disabled) |

## Strategy (deterministic, tunable)

Trend micro-breakout: **enter long** when `sma_fast(7) > sma_slow(25)`
AND the latest close breaks the prior `breakout_lookback(20)`-bar high.
Futures may take the mirror **short** on downtrend + breakdown; **spot is
long-only** (a spot SELL only closes/reduces a holding). Exits are
fixed-bps TP/SL (`+30 bps` / `-20 bps`). Thresholds live in
`SignalConfig` / `ScalpingRiskLimits`.

## Risk envelope (`ScalpingRiskLimits`, §5)

- Allowlist `XRPUSDT, DOGEUSDT, SOLUSDT`; `BTCUSDT` excluded (Futures
  MIN_NOTIONAL 50 > 10 cap).
- Max notional **10 USDT**/order (sizing floors, never rounds up).
- Caps: one open lifecycle per product+symbol, global open-lifecycle
  cap, daily order-count cap, daily realized-loss budget.
- Gates: spread (bps), data freshness, cooldown — all read from the
  ledger so they survive a fresh process / scheduler run.

## Safety boundaries

- **Demo hosts only**: `demo-api.binance.com` (spot) /
  `demo-fapi.binance.com` (futures), unsigned GETs. The adapter's
  `DEMO_DATA_HOSTS` allowlist fails closed; the live public adapter
  (which permits `api.binance.com`) is **banned** here.
- **No order mutation reachable**: an AST import guard
  (`tests/services/brokers/binance/demo_scalping/test_no_mutation_imports.py`)
  fails the build if the package *or this CLI* imports any execution
  client, signing helper, live-host adapter, ledger-internal repository,
  or the credential resolver.
- **No credentials needed**: the observe path is unsigned read-only.
- **No LLM** in the decision loop.

## Observe-only CLI

Default-disabled — with `BINANCE_DEMO_SCALPING_ENABLED` unset/false it
logs one line and exits 0 with zero side effects.

```bash
# Disabled (default): zero side effects, exit 0
uv run python scripts/binance_demo_scalping_signal.py

# Enabled: one-shot observe over the allowlist (spot)
BINANCE_DEMO_SCALPING_ENABLED=true \
  uv run python scripts/binance_demo_scalping_signal.py \
    --symbols XRPUSDT,DOGEUSDT,SOLUSDT --products spot --interval 1m --limit 50

# Spot + USD-M futures
BINANCE_DEMO_SCALPING_ENABLED=true \
  uv run python scripts/binance_demo_scalping_signal.py \
    --products spot,usdm_futures
```

Each line is an observe-only evidence record: `action` is always
`observe_only`; `would_enter` reports whether signal + risk *would* have
permitted entry (no order is placed). The ledger snapshot requires a DB
(`DATABASE_URL`); the market-data path itself needs neither DB nor creds.

Exit codes: `0` clean/disabled, `1` operator misconfiguration, `2`
runtime failure.

## Order-test filter validation (§5)

Min-notional/LOT_SIZE feasibility under the 10 USDT cap was validated
with the existing non-mutating order-test tooling (signed
`POST /order/test`, no order placed) — see
`docs/runbooks/binance-spot-demo-smoke.md` and
`docs/runbooks/binance-futures-demo-smoke.md`:

```bash
# Spot + Futures, per allowlisted symbol (requires Demo creds)
BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python scripts/binance_spot_demo_smoke.py --order-test --symbol XRPUSDT --cap-usdt 10
BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_futures_demo_smoke.py --order-test --symbol XRPUSDT --cap-usdt 10
```

All of XRP/DOGE/SOL clear MIN_NOTIONAL under the 10 USDT cap on both
products.

## One-shot executor (PR2)

PR2 adds the canonical **one-shot Demo executor** that consumes one
order-intent and runs a complete small Demo lifecycle to flat /
open-orders-0. It **opens and immediately closes** (no unattended
position — the §2 exit strategy is PR3).

| Module | Responsibility |
|---|---|
| `demo_scalping/order_intent.py` | `OrderIntent` + `build_order_intent` (signal → intent; pure, in the read-only package) |
| `demo_scalping_exec/reference.py` | Read-only demo-host exchangeInfo filters + ticker price (own fail-closed allowlist) |
| `demo_scalping_exec/executor.py` | `DemoScalpingExecutor.execute(intent, confirm=)` |
| `scripts/binance_demo_scalping_execute.py` | Default-disabled one-shot CLI |

**Flow:** live-ledger risk re-check (aborts before any order if blocked)
→ floor sizing (≤10 USDT, never round up) → open
(plan→preview→validate→submit) → fill-resolve (futures `NEW` → bounded
`GET /fapi/v1/order` poll, then non-flat positionRisk; ROB-305 §4) →
close (spot SELL of the free base / futures `reduceOnly` opposite side)
→ reconcile to flat / open-orders-0. A dirty reconcile records `anomaly`.

The executor lives **outside** the read-only import-guarded package
(it needs the signed execution clients + credentials). It reuses the
public execution clients, sizing helpers, and ledger service; the
audited smoke scripts stay an independent reference (dedupe follow-up).

### Executor CLI

Default-disabled; **dry-run unless `--confirm`** (dry-run sizes + risk-
checks with zero broker mutation):

```bash
# Disabled (default): exit 0, zero side effects
uv run python scripts/binance_demo_scalping_execute.py --product spot --symbol DOGEUSDT

# Dry-run (enabled, no orders): sizing + risk re-check only
BINANCE_DEMO_SCALPING_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py --product spot --symbol DOGEUSDT

# Real Demo lifecycle (requires --confirm + the product enable gate + Demo creds)
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_SPOT_DEMO_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py --product spot --symbol DOGEUSDT --confirm
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py --product usdm_futures --symbol XRPUSDT --confirm
```

Exit codes: `0` reconciled / dry-run / disabled, `1` blocked / operator
misconfig, `2` anomaly / runtime. An untrustworthy server market snapshot
(bookTicker/kline unavailable, malformed, or non-finite; ROB-841) is a
**blocked** outcome — the CLI emits a `demo_scalping_execute` evidence line
with `status=blocked` and `reason_codes=[market_conditions_unavailable]` and
exits `1` (never the generic exit `2`), before any DB session, executor, or
broker submit.

### Running the real-order smoke (clean ledger DB)

The executor's risk re-check enforces the durable caps (one open
lifecycle per product+symbol, **global** open-lifecycle cap, daily
counts) against `binance_demo_order_ledger`. Run the live `--confirm`
smoke against a **clean demo ledger DB** so phantom/residual open rows
don't (correctly) trip the global cap. Procedure:

1. Point `DATABASE_URL` at the clean demo ledger DB; confirm
   `count_open_lifecycles()` is low / 0.
2. Export the shared Demo creds (`BINANCE_DEMO_API_KEY` /
   `BINANCE_DEMO_API_SECRET`) and the product enable flag. Never print
   or commit values.
3. Spot: `--product spot --symbol DOGEUSDT --confirm` → expect
   `status=reconciled`, open orders 0.
4. Futures: `--product usdm_futures --symbol XRPUSDT --confirm` → expect
   `status=reconciled`, position flat, open orders 0.
5. Capture the redacted evidence line + a broker/ledger reconciliation
   check (open orders 0; futures position flat).

### Abandoned root-reservation reconciliation (ROB-844)

`reserve_root_planned` commits its exposure claim in an independent DB
transaction before broker order submission. A process crash can therefore leave a durable
`planned` root. It is never released by elapsed time alone. The scheduleless
TaskIQ entrypoint `binance.demo_root_reservation.reconcile` combines an age
eligibility guard with exact broker truth by `client_order_id`. Before any
broker GET, the persisted row must match the product's exact Demo host and its
`extra_metadata.credential_fingerprint` must exactly match the running
adapter's opaque API-key fingerprint. Raw API keys/secrets are never stored.
Legacy rows with no fingerprint, rotated/mismatched credentials, or unavailable
client identity stay blocking and dispatch zero broker reads.

- explicit Binance `-2013` order-not-found is releasable only while reservation
  age is **strictly below 89 days**. At exactly 89 days or older, lookup
  retention makes absence ambiguous and the row stays blocking;
- a real terminal payload can independently release at any age only when the
  broker body actually contains valid `clientOrderId`, `symbol`, `status`,
  non-negative finite `executedQty`, and a positive numeric `orderId`; identity
  must match and executed quantity must be zero. Spot accepts documented
  `CANCELED`/`REJECTED`/`EXPIRED`/`EXPIRED_IN_MATCH`; Futures accepts
  `CANCELED`/`REJECTED`/`EXPIRED`;
- open/partial/filled, missing/blank/invalid required fields, undocumented or
  cross-product statuses, authentication/transport/5xx/other 4xx → keep
  blocking;
- discovery is lock-free; each candidate then gets its own independent
  transaction and one-row `FOR UPDATE SKIP LOCKED`. Slow broker I/O for row 1
  never locks row 2, while executor-first/reconciler-first races remain safe.

The task has no schedule and uses three default-off gates. The minimum age is
one hour by default and has a hard one-hour floor; configuring a larger value is
allowed. With confirm off, it performs broker reads and reports
`would_release`, with **zero ledger mutation/write** (the read-only transaction
may still complete normally).

| Env var | Effect |
|---|---|
| `BINANCE_DEMO_SCALPING_ENABLED` | shared master capability; required |
| `BINANCE_DEMO_RESERVATION_RECONCILE_ENABLED` | enables the manual reconcile read pass |
| `BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM` | permits local cancelled→reconciled mutation; unset means dry-run |
| `BINANCE_DEMO_RESERVATION_RECONCILE_MIN_AGE_SECONDS` | candidate age, default/minimum `3600` |

Spot and Futures clients are constructed independently. Configure only the
product lanes this worker is authorized to read (`BINANCE_SPOT_DEMO_*` and/or
`BINANCE_FUTURES_DEMO_*`). A missing, disabled, or invalid sibling lane does not
broaden required credentials: its candidates report `client_unavailable`, stay
blocking, and receive zero broker GETs.

These settings are read by the **running TaskIQ worker**, not by the enqueue
client. Prefixing `VAR=value` to `taskiq kick` changes only the kick process and
does not propagate gates or credentials to an existing worker.

1. Dry-run window: set the worker deployment environment with the two read
   gates, only the authorized product Demo gate(s)/credentials, minimum age, and
   `BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM=false`; restart/reload the TaskIQ
   worker and verify it is healthy.
2. Enqueue with no environment prefix:

   ```bash
   uv run taskiq kick app.core.taskiq_broker:broker \
     binance.demo_root_reservation.reconcile
   ```

3. Inspect the task result/log: require `status=ok`, expected `scanned` and
   `would_release` outcomes, and `released=0`. Query the ledger and confirm all
   candidate lifecycle states/timestamps/metadata are unchanged. Cross-check
   broker open orders (and Futures positions) under the same credentials.
4. Apply window: change only the running worker deployment setting to
   `BINANCE_DEMO_RESERVATION_RECONCILE_CONFIRM=true`, restart/reload the worker,
   confirm the effective setting, then issue the same plain kick exactly once.
5. Verify each reported `released` row is now `reconciled` with matching
   redacted reconciliation evidence; verify kept rows remain `planned`, broker
   open orders are expected, and Futures positions are understood.
6. **Immediately close the apply window:** set confirm back to `false`, restart/
   reload the worker, and verify a subsequent controlled invocation reports
   `mutation_confirmed=false`. On any error or unexpected outcome, perform this
   rollback first and do not retry until ledger/broker truth is reconciled.

## Bounded-monitor TP/SL (`execute_monitored`)

The TP/SL exit is a **bounded app-managed monitor** (MARKET-only). After
entry, poll the bookTicker within a bounded window and MARKET-close on a
TP/SL cross, else failsafe-close at the window end — the run **always ends
flat** (no unattended position).

> **Why not broker-side brackets?** That approach was attempted (futures
> STOP_MARKET/TAKE_PROFIT_MARKET, spot OCO) but **demo-fapi rejects
> conditional orders** via `/fapi/v1/order` with `-4120 "Order type not
> supported for this endpoint. Please use the Algo Order API endpoints
> instead."` (live-confirmed 2026-05-25), even though `exchangeInfo`
> advertises the types. The bracket path was removed; the bounded monitor
> uses only MARKET orders, which are proven on demo.

| Surface | Behaviour |
|---|---|
| `demo_scalping_exec/executor.py` | `execute_monitored(intent, …, max_poll_count/poll_interval_s/max_runtime_s)` — open → bounded poll → TP/SL MARKET-close or failsafe → reconcile flat. Shares `_open_position` + `_close_and_reconcile` with `execute()`. |
| `scripts/binance_demo_scalping_execute.py` | `--monitor` (+ `--max-poll-count`) flag |

**Lifecycle:** open (MARKET) → fill-resolve → monitor (poll mid price;
`mid≥tp`→`take_profit`, `mid≤sl`→`stop_loss`, bounds exhausted→`timeout`)
→ MARKET close (spot SELL free / futures `reduceOnly`) → reconcile to
flat / open-orders-0. `result.exit_reason` records which path fired. TP/SL
prices are `entry ± bps`, tick-aligned (`reference.tick_size`).

```bash
# Monitored entry (real; clean ledger DB + Demo creds + product gate)
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py \
    --product usdm_futures --symbol XRPUSDT --monitor --confirm
# → status=reconciled, exit_reason ∈ {take_profit, stop_loss, timeout}, flat
```

### Live validation (operator, clean env)

Unit tests cover TP/SL/timeout decisions + close+reconcile with a fake
broker + scripted prices. Live-validate against a **clean demo ledger DB**:
a `--monitor --confirm` entry ends `status=reconciled` with `exit_reason`
set, position flat, open orders 0. Capture redacted evidence.

## Scheduler scaffold (PR4) — default-OFF, no activation

PR4 adds the scheduler **scaffold**: one tick runs the signal per
allowlisted symbol and places a **bounded-monitor entry** (which always
exits flat in-run) on each signal. It is **registered with no schedule**
(a manual entry point only) and **default-OFF** behind a two-key flag gate.

| Surface | Added |
|---|---|
| `app/services/.../demo_scalping_exec/scheduler.py` | `run_scalping_tick` — signal-driven `execute_monitored` entries; `enabled=False` kill switch; per-item error collection (failure-only) |
| `app/jobs/binance_demo_scalping_runner.py` | `run_demo_scalping_tick` — env-wired, two-key gate |
| `app/tasks/binance_demo_scalping_tasks.py` | `@broker.task("binance.demo_scalping.tick")` — **no `schedule=`** |

### Flags (all default-OFF)

| Env var | Effect |
|---|---|
| `BINANCE_DEMO_SCALPING_ENABLED` | shared feature gate |
| `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED` | scheduler **kill switch** |
| `BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM` | second key — without it the tick runs signals + risk re-checks but every `execute_monitored` is a **dry-run** (zero broker mutation) |

Both `*_ENABLED` flags must be truthy or the tick builds zero clients,
touches no DB, and returns `{"status": "disabled"}`. **The kill switch is:
unset `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED`** (or set it false) — the
next tick is an immediate no-op.

### Manual invocation (no schedule registered)

```bash
# Dry-run tick (signals + risk, zero orders): scheduler on, confirm off
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED=true \
  uv run taskiq kick app.core.taskiq_broker:broker binance.demo_scalping.tick

# Real tick (places monitored entries): add the second key + product gates
# + creds, and run against a CLEAN demo ledger DB
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED=true \
BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM=true \
BINANCE_SPOT_DEMO_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run taskiq kick app.core.taskiq_broker:broker binance.demo_scalping.tick
```

### Scheduler entrypoint CLI (`scripts/binance_demo_scalping_tick.py`)

`scripts/binance_demo_scalping_tick.py` is the operator-facing, **env-driven**
one-tick entrypoint an external scheduler shells out to. It wraps
`run_demo_scalping_tick`, prints a single-line JSON summary to stdout, and maps
it to an exit code (0 = disabled/clean, 1 = ran-with-errors or runner raised).
No flags — the same two-key gate + `_CONFIRM` env vars above apply.

```bash
# Gate OFF (default) → no-op, zero clients, exit 0
uv run python -m scripts.binance_demo_scalping_tick
# → {"base_enabled": false, "scheduler_enabled": false, "status": "disabled"}

# Dry-run tick (signals + risk, zero orders)
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED=true \
  uv run python -m scripts.binance_demo_scalping_tick
```

This is the command the (paused) Prefect deployment in `robin-prefect-automations`
invokes; the Prefect repo owns scheduling/retry/alerting and only ever calls
this CLI (it never imports auto_trader trading logic).

### Activation — separate operator gate

No recurring schedule is registered by this repo. Production recurrence
(`paused=false`) lives in `robin-prefect-automations` as a **paused-by-default**
deployment that shells out to the tick CLI above. **Do not activate** without
the §"Hard safety boundaries" gates satisfied + explicit operator
approval. Failure-only alerting wires onto the tick's error log
(`logger.error` / the `TickSummary.errors` list) and the CLI's non-zero exit; a
clean tick is quiet. Rollback = unset `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED`
(kill switch) and pause the deployment in the ops repo.
