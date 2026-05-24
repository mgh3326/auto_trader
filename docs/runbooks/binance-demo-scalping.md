# Binance Demo Scalping — Observe-Only Signal Runner (ROB-307 PR1)

PR1 of the Binance Demo scalping MVP: a **read-only, observe-only**
deterministic signal runner plus the risk/order-intent contract and
ledger-backed durable state. **It never places, previews, or tests an
order.** Later PRs add execution (PR2), TP/SL (PR3), and the default-OFF
scheduler scaffold (PR4).

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
misconfig, `2` anomaly / runtime.

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

## Broker-side bracket TP/SL (PR3)

PR3 adds the §2-preferred **broker-side bracket**: after entry, place
exchange-native exits and **hold the protected position** (it survives
across scheduler runs — that's the point). Exit detection + cleanup is a
separate reconcile step.

| Surface | Added |
|---|---|
| `futures_demo` exec client | `submit_reduce_only_trigger` (STOP_MARKET / TAKE_PROFIT_MARKET, reduceOnly) |
| `spot_demo` exec client | `submit_oco` (SELL OCO: TP limit + SL stop-limit) |
| `demo_scalping_exec/reference.py` | `tick_size` (PRICE_FILTER) for tick-aligned bracket prices |
| `demo_scalping_exec/executor.py` | `execute_bracket` (open + place TP/SL + hold) and `reconcile_bracket` (detect exit, cancel survivor, close+reconcile) |
| `scripts/binance_demo_scalping_execute.py` | `--bracket` and `--reconcile OPEN_CID` modes |

**Lifecycle:** `execute_bracket` → open (MARKET) → fill-resolve → place
TP+SL (futures: two reduceOnly triggers; spot: one OCO, tick-aligned) →
`record_filled` with the bracket leg ids in `extra_metadata` → status
`bracketed` (parent stays `filled`, exits resting). Unproven fill →
`anomaly` (no bracket). Bracket-placement failure → best-effort failsafe
close + `anomaly` (never left unprotected). On a later tick,
`reconcile_bracket` → still holding = `still_protected` (no-op); exit
fired = cancel any surviving futures leg (spot OCO self-cancels) →
parent `filled→closed→reconciled`.

```bash
# Open + bracket + HOLD (real; clean ledger DB + Demo creds + product gate)
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py \
    --product usdm_futures --symbol XRPUSDT --bracket --confirm
# → status=bracketed; note the open_client_order_id from the evidence line

# Later: reconcile the held position by its open client_order_id
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run python scripts/binance_demo_scalping_execute.py \
    --product usdm_futures --symbol XRPUSDT --reconcile rob307-<...>
# → still_protected (held) OR reconciled (exit fired, survivor cancelled)
```

### Live demo trigger-firing validation (operator, clean env)

Unit tests cover placement + cancellation + reconcile logic with a fake
broker; they **cannot** prove Binance Demo actually *fires* a STOP/TP
trigger (needs real price movement). Validate live in a clean demo ledger
DB:

1. `--bracket --confirm` a tiny position; confirm both exit legs rest
   (`GET openOrders` shows STOP_MARKET + TAKE_PROFIT_MARKET, or the OCO
   list) and the position is held.
2. Wait for (or nudge via a near-the-money trigger) one leg to fire;
   confirm the position goes flat.
3. `--reconcile <open_cid>`; confirm the surviving leg is cancelled
   (futures), open orders = 0, and the parent ledger row reconciles.
4. Capture redacted evidence (order ids, leg statuses, final flat /
   open-orders-0).

## Scheduler scaffold (PR4) — default-OFF, no activation

PR4 adds the scheduler **scaffold**: one tick reconciles held bracketed
positions then runs the signal per allowlisted symbol and places brackets
on entries. It is **registered with no schedule** (a manual entry point
only) and **default-OFF** behind a two-key flag gate.

| Surface | Added |
|---|---|
| `app/services/.../demo_scalping_exec/scheduler.py` | `run_scalping_tick` — reconcile held + signal-driven bracket entries; `enabled=False` kill switch; per-item error collection (failure-only) |
| `app/jobs/binance_demo_scalping_runner.py` | `run_demo_scalping_tick` — env-wired, two-key gate |
| `app/tasks/binance_demo_scalping_tasks.py` | `@broker.task("binance.demo_scalping.tick")` — **no `schedule=`** |
| ledger service | `list_held_bracketed` (rows in `filled` = held) |

### Flags (all default-OFF)

| Env var | Effect |
|---|---|
| `BINANCE_DEMO_SCALPING_ENABLED` | shared feature gate |
| `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED` | scheduler **kill switch** |
| `BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM` | second key — without it the tick runs signals + risk re-checks but every `execute_bracket` is a **dry-run** (zero broker mutation) |

Both `*_ENABLED` flags must be truthy or the tick builds zero clients,
touches no DB, and returns `{"status": "disabled"}`. **The kill switch is:
unset `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED`** (or set it false) — the
next tick is an immediate no-op.

### Manual invocation (no schedule registered)

```bash
# Dry-run tick (signals + risk, zero orders): scheduler on, confirm off
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED=true \
  uv run taskiq kick app.core.taskiq_broker:broker binance.demo_scalping.tick

# Real tick (places brackets): add the second key + product gates + creds,
# and run against a CLEAN demo ledger DB
BINANCE_DEMO_SCALPING_ENABLED=true BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED=true \
BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM=true \
BINANCE_SPOT_DEMO_ENABLED=true BINANCE_FUTURES_DEMO_ENABLED=true \
  uv run taskiq kick app.core.taskiq_broker:broker binance.demo_scalping.tick
```

### Activation — separate operator gate

No recurring schedule is registered by this repo. Production recurrence
(`paused=false`) is an operations decision: TaskIQ cron or a Prefect
deployment in `robin-prefect-automations`. **Do not activate** without
the §"Hard safety boundaries" gates satisfied + explicit operator
approval. Failure-only alerting wires onto the tick's error log
(`logger.error` / the `TickSummary.errors` list); a clean tick is quiet.
Rollback = unset `BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED` (kill switch)
and pause/disable the schedule in the ops repo.
