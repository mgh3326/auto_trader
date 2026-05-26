# Binance Demo WebSocket Scalping Daemon — Runbook

**Scope.** Operator runbook for the Binance Demo WebSocket Scalping Daemon (`scripts/binance_demo_scalping_ws_daemon.py`). This daemon consumes real-time market data to identify breakout scalping opportunities on Binance USD-M Perpetual Futures, and places confirm-gated, concurrency-guarded mock orders on the USD-M Futures Demo lane (`demo-fapi.binance.com`).

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
| `true` | `true` | `true` | **Confirmed Demo Mode**. Real mock orders are placed on `demo-fapi.binance.com`. |

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

To engage active trading on the Demo backend, add `BINANCE_DEMO_SCALPING_WS_CONFIRM=true`:

```bash
export BINANCE_DEMO_SCALPING_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_ENABLED=true
export BINANCE_DEMO_SCALPING_WS_CONFIRM=true

uv run python -m scripts.binance_demo_scalping_ws_daemon
```

**Risk Safeguards:**
* Real orders are executed against the Demo endpoint `demo-fapi.binance.com`.
* Subject to **authoritative live-ledger risk checks** (`_preflight`) inside the executor.
* Guided by an **in-process concurrency lock** (max `1` global position open at any time) to prevent race conditions or duplicate entries under fast websocket updates.
* Writes full order histories to `binance_demo_order_ledger` and telemetry to `scalp_trade_analytics`.

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
