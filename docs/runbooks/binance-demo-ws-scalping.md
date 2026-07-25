# Binance Demo WebSocket Scalping Daemon — Runbook

**Scope.** Operator runbook for the Binance Demo WebSocket Scalping Daemon (`scripts/binance_demo_scalping_ws_daemon.py`). This daemon consumes real-time market data to identify breakout scalping opportunities on Binance USD-M Perpetual Futures, and places confirm-gated, concurrency-guarded mock orders on the USD-M Futures Demo lane (`demo-fapi.binance.com`).

> **⚠️ ROB-316 finding (2026-05-26) — this daemon is plumbing/harness, NOT a validated-alpha runner.**
>
> The trend micro-breakout signal this daemon executes has **no durable backtested edge**. Backtested on ~60 days of XRPUSDT spot tick data (232 trades, NautilusTrader, conservative fills) it is net-negative at realistic fees and **gross-negative even before fees**; widening targets (100/100) and adding an ICT session/killzone filter did not help — a 14-day apparent edge **vanished out-of-sample**. See [ROB-316](https://linear.app/mgh3326/issue/ROB-316) and `docs/plans/ROB-316-*`.
>
> **Operating posture until a validated signal exists:**
> - Treat this daemon (and the 5-min Prefect tick in `binance-demo-scalping.md`) as **plumbing / observe / a harness for a future validated signal** — not a strategy-alpha runner.
> - The current micro-breakout signal must remain **disabled or observe/dry-run only**.
> - `confirm=true` and any recurring activation (Prefect / scheduler / launchd / TaskIQ) require a **separate validated-signal gate**. Do not enable live Demo order confirmation on this signal.
>
> **ROB-905 — the validated-signal gate is now enforced in code** for the 5-min Prefect tick path (`run_demo_scalping_tick`). `BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM=true` is honoured only when `BINANCE_DEMO_SCALPING_VALIDATED_GATE_PATH` points at a readable `validated_signal_gate.v1` / `verdict: "validated"` (optional future `valid_until`) artifact; otherwise confirm is fail-closed **downgraded to dry-run**. Since no validated signal exists yet, that artifact should not exist and the tick stays dry-run. See `binance-demo-scalping.md` → "Validated-signal gate for `confirm=true`".

---

## 1. Lane boundaries and Scope

* **Public Market Data:** Real-time best-bid/ask quotes and closed 1-minute klines are read-only from the public stream host `fstream.binance.com`. No credentials or subscriptions are required to ingest these public streams.
* **Order Mutation:** Order placement and positions are restricted exclusively to the **Demo Futures** environment (`demo-fapi.binance.com`).
* **Mainnet & Testnet Safe:** Hard fail-closed guards at the client transport layer prevent signed requests from reaching production mainnet (`fapi.binance.com`) or deprecated futures testnet (`testnet.binancefuture.com`).

---

## 2. Preconditions

Before starting the daemon, ensure the following requirements are met:

1. **Credentials Present:** Either the Futures-specific credentials (`BINANCE_FUTURES_DEMO_API_KEY` / `BINANCE_FUTURES_DEMO_API_SECRET`) or the canonical shared Demo credentials (`BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`) must be resolved.
2. **Database Reachable:** The local or production PostgreSQL instance must be running, and the database URL (`DATABASE_URL`) must be configured.
3. **Database Ledger Migrated:** Ensure all database migrations are applied:
   ```bash
   uv run alembic upgrade head
   ```
   (Reuses the existing `binance_demo_order_ledger` and `scalp_trade_analytics` tables.)

> **⚠️ Credential loading — do NOT blindly `source shared/.env.prod.native`.**
> That file is the native production service's full env (it may carry mainnet/
> testnet and unrelated secrets). Blindly `set -a; source ...` into an
> interactive shell pollutes your session, can export mainnet/testnet vars, and
> risks echoing secrets. Instead use **dotenv-aware / native-service env
> loading**: let the launchd/native service load its own env file, or load only
> the Demo-prefixed keys (`BINANCE_FUTURES_DEMO_*` / `BINANCE_DEMO_*`) into a
> scoped subprocess — never `BINANCE_TESTNET_*` (which do nothing for Demo) —
> and never print credential values. The daemon resolves Demo credentials via
> `resolve_demo_credentials()`; only those keys are needed.

---

## 3. The Three Gates

The behavior of the WebSocket daemon is strictly gated by three environment variables:

| Gate | Type | Description |
|---|---|---|
| `BINANCE_DEMO_SCALPING_ENABLED` | Boolean | Central master kill-switch for all Binance demo scalping systems. |
| `BINANCE_DEMO_SCALPING_WS_ENABLED` | Boolean | Activates the WebSocket-driven daemon lane specifically. |
| `BINANCE_DEMO_SCALPING_WS_CONFIRM` | Boolean | Master confirmation switch for order placement. If `false`, the daemon operates in Dry-Run mode. |

### Gate Behavior Matrix

| `BINANCE_DEMO_SCALPING_ENABLED` | `BINANCE_DEMO_SCALPING_WS_ENABLED` | `BINANCE_DEMO_SCALPING_WS_CONFIRM` | Resulting Behavior |
|:---:|:---:|:---:|---|
| `false` / unset | (any) | (any) | **Disabled**. Exits instantly, prints JSON metadata, opens no connections. |
| `true` | `false` / unset | (any) | **Disabled**. Exits instantly, prints JSON metadata, opens no connections. |
| `true` | `true` | `false` / unset | **Dry-Run**. Subscribes to websocket streams, runs signal generation, but passes `confirm=False` to the executor (zero broker mutation). |
| `true` | `true` | `true` | **Confirmed-eligible**. Real mock orders are placed on `demo-fapi.binance.com` **only if the CLI also passes `--confirm` and a trigger bound** (§4.3/§4.4). The env gate alone runs dry-run. |

> The `--confirm` CLI flag is an **independent** second gate on top of the env var: the flag alone never enables mutation, and the env var alone never enables mutation. Confirmed runs additionally **must** be bounded (`--max-triggers` / `--exit-after-first-trigger`) or the daemon exits 2 without placing an order.

---

## 4. Operational Modes

### 4.1. Default-Disabled Startup (Inert)

With no environment variables set, the daemon runs in a completely inert state, printing its status and exiting with `0`:

```bash
uv run python -m scripts.binance_demo_scalping_ws_daemon
```

Output:
```json
{"base_enabled": false, "status": "disabled", "subscribed": false, "ws_enabled": false}
```

### 4.2. Dry-Run Mode (Observation)

To verify connection stability, signal generation, and data freshness without mutating your account balance:

```bash
export BINANCE_DEMO_SCALPING_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_CONFIRM=false

uv run python -m scripts.binance_demo_scalping_ws_daemon
```

**What to expect in Dry-Run:**
* Prints `{"status": "running", "subscribed": false, ...}` to stdout.
* Connects to `wss://fstream.binance.com` for allowed symbols (e.g. `XRPUSDT`).
* Computes breakout signals in memory.
* When a trigger is hit, runs the `WsExecutionBridge` and `DemoScalpingExecutor` with `confirm=False`.
* Logs trigger events with `confirm=False status=None` or `status=dry_run`.

### 4.3. Confirmed Demo Mode (Active Trading)

Confirmed mode requires **all three**: the env gate `BINANCE_DEMO_SCALPING_WS_CONFIRM=true`, the explicit `--confirm` flag, **and** a trigger bound (`--max-triggers` / `--exit-after-first-trigger`). Missing the flag → dry-run; missing the bound → fail-closed (exit 2, no order). See §4.4 for the bounded first-live procedure.

```bash
export BINANCE_DEMO_SCALPING_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_CONFIRM=true

# --confirm AND a trigger bound are both mandatory for real orders.
uv run python -m scripts.binance_demo_scalping_ws_daemon --confirm --max-triggers 1
```

**Risk Safeguards:**
* Real orders are executed against the Demo endpoint `demo-fapi.binance.com`.
* Subject to **authoritative live-ledger risk checks** (`_preflight`) inside the executor.
* Guided by an **in-process concurrency lock** (max `1` global position open at any time) to prevent race conditions or duplicate entries under fast websocket updates.
* Writes full order histories to `binance_demo_order_ledger` and telemetry to `scalp_trade_analytics`.

### 4.4. Bounded Operator Mode (First-Live Validation)

The daemon is a long-running loop by default. For a **first-live validation of the plumbing/harness** (real `fstream` ingest → state → trigger → bridge), use the bounded flags so the run is small, observable, and self-terminating:

| Flag | Effect |
|---|---|
| `--max-runtime-sec N` | Wall-clock cap — exits cleanly after N seconds (cancels even a quiet stream). |
| `--max-triggers N` | Exits cleanly after N triggers (count survives reconnects). |
| `--exit-after-first-trigger` | Shorthand for `--max-triggers 1`. |
| `--confirm` | Required **in addition to** `BINANCE_DEMO_SCALPING_WS_CONFIRM=true` to place real Demo orders. Confirmed runs **must** also carry a trigger bound or the daemon **fails closed (exit 2, no order)**. |

> **Scope reminder (ROB-316):** this validates *plumbing*, not alpha. The current micro-breakout signal has no backtested edge — keep first-live validation **dry-run**. Only do a confirmed bounded run if you specifically need to prove the order-placement path end-to-end against Demo, and keep it to a single trigger.

**Step 1 — bounded dry-run (real fstream, no orders, time-boxed):**
```bash
# Load ONLY Demo creds into a scoped subprocess (see Preconditions caveat) —
# do not blindly `source` the prod env file into your shell.
BINANCE_DEMO_SCALPING_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_ENABLED=true \
uv run python -m scripts.binance_demo_scalping_ws_daemon --max-runtime-sec 120
```
Subscribes to `wss://fstream.binance.com`, evaluates triggers, runs the executor with `confirm=False` (zero mutation), and exits after 120s. Confirm clean reconnect/freshness logs and no errors.

**Step 2 — confirmed single-trigger plumbing check (real Demo order, bounded):**
```bash
BINANCE_DEMO_SCALPING_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_CONFIRM=true \
uv run python -m scripts.binance_demo_scalping_ws_daemon --confirm --exit-after-first-trigger
```
Places at most **one** real Demo order on `demo-fapi.binance.com`, then exits. Omitting `--confirm` (or the env gate) → dry-run. Omitting a trigger bound while confirmed → **exit 2, no order placed**. Verify the resulting `binance_demo_order_ledger` lifecycle + `scalp_trade_analytics` row (§5) and confirm it reconciled (no `anomaly`).

---

## 5. Monitoring and Health Checks

The daemon exposes health snapshots that can be polled or verified via standard logs.

1. **Staleness Tracking:** Ensure `data_age_seconds` logged in triggers remains low (< 5 seconds). If a quote or kline is older than the limits permit (e.g. 120s), the supervisor automatically drops the trigger with `reason=STALE_DATA`.
2. **Freshness Monitoring:** Look for periodic state updates and kline ingestion logs in stdout.
3. **Database Spot-Checking:** Verify ledger records:
   ```sql
   SELECT * FROM binance_demo_order_ledger ORDER BY created_at DESC LIMIT 5;
   ```

---

## 6. Stop and Rollback Procedures

### 6.1. Stopping the Daemon
Simply send a `SIGINT` (`Ctrl+C`) or `SIGTERM` to the process. The supervisor will close all streaming websocket connections and clean up any in-memory state cleanly.

### 6.2. Rollback or Deactivation
To deactivate the WebSocket daemon lane without deleting config files, set the specific enable gate to false:
```bash
export BINANCE_DEMO_SCALPING_WS_ENABLED=false
```

### 6.3. Manual Reconciliation
If the daemon is terminated while a position is open, or if you need to manually reconcile/close a stale position:
1. Reconcile or smoke-test via the USD-M Futures smoke CLI using the `reduceOnly` mode:
   ```bash
   uv run python -m scripts.binance_futures_demo_smoke --close-position --symbol XRPUSDT --confirm
   ```

---

## 7. Failure Recovery and Troubleshooting

* **Stream Disconnection:** The daemon implements automatic reconnects using jittered exponential backoff (per ROB-285). It attempts to reconnect continuously; however, if the consecutive failure threshold is breached, the supervisor will bubble up the exception and exit `1` so an external orchestrator (like systemd or Kubernetes) can restart it.
* **Stale Quote block:** If you see `trigger suppressed symbol=XRPUSDT reason=STALE_DATA`, it means the WebSocket stream received a kline but the best-bid/ask quote ticker hasn't updated recently. This is a safety feature. Check if the WebSocket connection is laggy or if the market is illiquid.
* **Concurrency Skip:** If you see `ws_bridge skip symbol=XRPUSDT reason=OPEN_LIFECYCLE_EXISTS`, it means a trade is already in progress for that symbol. If the global cap is hit, it logs `GLOBAL_LIFECYCLE_CAP_REACHED`. This is a desired safety behavior ensuring we do not double-enter.
* **Database/Credentials Unavailable:** If PostgreSQL or the credentials fail, the daemon fails closed immediately and logs the error before subscribing.

---

## 8. Relationship to the 5-Minute Polling Tick

The pre-existing 5-minute polling tick (run via TaskIQ / Prefect / scheduler) remains completely active and untouched by the WebSocket daemon. The 5-minute tick is now reclassified as a **polling intraday / smoke observation path**. Pausing the 5-minute polling scheduler is a deferred operator decision and is not required for the WebSocket daemon to run safely.
