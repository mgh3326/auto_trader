# Binance Demo WebSocket Scalping Daemon (ROB-317) — Design + Runbook

**Status.** Design / slice 1. No daemon code ships with this document; the
module skeleton, CLI, bridge, and tests land in later slices (see §13).

**Scope.** Design and operator runbook for a long-running, WebSocket-driven
scalping **daemon** that replaces the 5-minute Prefect polling tick as the
*true* scalping hot path for the Binance **Futures Demo** lane
(`demo-fapi.binance.com`). The daemon consumes live public futures market
streams read-only, evaluates triggers event-by-event, and routes confirmed
entries through the **existing** demo scalping risk → executor → ledger →
analytics → review layers. Order mutation never leaves `demo-fapi.binance.com`.

This document satisfies ROB-317 Scope §1 (design first) and the design/runbook
acceptance criteria. Related work: ROB-307 (demo scalping MVP), ROB-313/ROB-315
(cost-capture + analytics + review loop), ROB-285 (public market data WS,
read-only), ROB-298 (demo execution clients + unified ledger).

---

## 1. Why the 5-minute Prefect flow is polling, not scalping

Today's path:

```text
Prefect every 5 min
  -> python -m scripts.binance_demo_scalping_tick
  -> REST polling: 1m kline / bookTicker  (demo-fapi, via DemoScalpingMarketData)
  -> deterministic signal (demo_scalping/signal.py)
  -> DemoScalpingExecutor (demo_scalping_exec/)
  -> binance_demo_order_ledger / scalp_trade_analytics / scalping_daily_reviews
```

A 5-minute REST tick is a **low-frequency intraday/day-trading** cadence. By the
time a tick fires, a scalp-sized move (seconds to low minutes) has already come
and gone. The cadence is bounded by the scheduler interval, not by market
events. This is fine for observation, smoke validation, and reconciliation —
but it is structurally incapable of *true scalping*, which must react to market
events the moment they occur.

**This flow is retained, not deleted.** It is reclassified as **polling
intraday / compatibility / smoke path**. The WS daemon is the true scalping hot
path. Pausing the Prefect tick is an operator decision deferred until the daemon
is validated (see §11.6).

Target path:

```text
launchd/native (dev) | container/systemd (prod) long-running service
  -> live public futures WS streams (fstream.binance.com, read-only, unsigned)
  -> in-memory per-symbol MarketState + per-symbol event queues
  -> event-driven trigger (reuses demo_scalping signal + risk contract)
  -> async concurrency guard (per-symbol lock + global semaphore)
  -> ws_bridge -> existing DemoScalpingExecutor (demo-fapi, confirm-gated)
  -> binance_demo_order_ledger / scalp_trade_analytics / scalping_daily_reviews
```

---

## 2. Lane boundaries — market data vs order mutation

The daemon deliberately splits the **read-only market-data host** from the
**signed order-mutation host**. These are different hosts and different
allowlists, and they must never be conflated.

| Lane | Host | Auth | Used by daemon for | Status |
|---|---|---|---|---|
| **Public futures stream (read)** | **`fstream.binance.com`** | unsigned | aggTrade / bookTicker / kline market data | **New read-only WS allowlist (this issue)** |
| **Futures Demo (mutate)** | **`demo-fapi.binance.com`** | signed (`BINANCE_FUTURES_DEMO_*`) | order submit / cancel / reconcile | Existing (ROB-298 PR 2) |
| Spot Demo | `demo-api.binance.com` | signed | — (not used by this daemon) | Existing |
| Live futures (signed) | `fapi.binance.com`, `fstream.binance.com` | signed | **never** | Refused fail-closed (signed deny-list) |
| Futures testnet | `testnet.binancefuture.com` | — | **never** | Refused fail-closed (deprecated) |

### 2.1 Why market data rides a live public host (locked decision)

There is **no demo-only futures market WS host**. `demo-fapi.binance.com` serves
signed order/account REST; it does not expose a public market stream. The only
public futures WS is the live `fstream.binance.com`.

**Decision (ROB-317): read-only live public futures WS for market data; orders
stay demo-only.** Rationale:

* The stream is **unsigned, read-only public market data** — the same feed every
  client reads. No credentials, no mutation, no account exposure.
* It reuses the ROB-285 `BinancePublicWSClient` pattern instead of inventing a
  parallel client.

**Accepted tradeoff — price-source mismatch.** The signal is evaluated on the
**live** public book while fills land on the **demo** book, which can diverge
(thin/synthetic demo liquidity). This is acceptable because the demo lane's
purpose is to validate the *execution plumbing and safety contract*, not PnL
fidelity. This tradeoff is stated here so it is not mistaken for a bug later.

### 2.2 Hard invariants

* A **new** `PUBLIC_FUTURES_STREAM_HOSTS = {"fstream.binance.com"}` allowlist
  gates the daemon's WS client. It is **read-only/unsigned** and physically
  separate from the futures-demo signed transport.
* `fstream.binance.com` **remains in the futures-demo signed-transport
  deny-list.** A signed request to `fstream` still raises
  `BinanceFuturesDemoCrossAllowlistViolation` / `BinanceLiveHostBlocked`. The
  read-only WS allowlist (`{fstream.binance.com}`) and the signed mutation
  allowlist (`FUTURES_DEMO_HOSTS = {demo-fapi.binance.com}`) are **disjoint** —
  no host is both read-allowed *and* signed-allowed. Tests assert (a) that
  disjointness and (b) that the signed transport still rejects `fstream` even
  though the read path permits it.
* Order mutation only ever signs against `FUTURES_DEMO_HOSTS =
  {"demo-fapi.binance.com"}` — unchanged from ROB-298.
* No live order path, no live signed host, no testnet host. Ever.

---

## 3. Module boundary & package layout

The existing **read-only signal package** vs **mutation execution package** split
(ROB-307, AST-guarded) is preserved and extended. The daemon's read-only hot
path is a new package; the trigger→executor bridge lives on the **exec** side.

```text
app/services/brokers/binance/demo_scalping_ws/        # NEW — read-only, no signed clients
  market_stream.py   # futures WS subscriber; wraps/extends BinancePublicWSClient;
                     #   reconnect + exponential backoff; aggTrade + bookTicker (+ kline_1m)
  state.py           # per-symbol MarketState: last bid/ask/trade/kline + per-stream recv ts
  signal.py          # event-driven trigger; reuses demo_scalping/signal.py + contract.py (pure)
  supervisor.py      # asyncio event loop, per-symbol queues, lifecycle, heartbeat, debounce
  health.py          # status snapshot (JSON) + stale-data checks

app/services/brokers/binance/demo_scalping_exec/      # EXISTING — mutation side
  ws_bridge.py       # NEW — trigger -> live ledger risk re-check -> DemoScalpingExecutor

scripts/
  binance_demo_scalping_ws_daemon.py                  # NEW — default-disabled CLI entrypoint
```

### 3.1 Import-guard contract

`tests/services/brokers/binance/demo/test_no_testnet_imports.py` gains an
assertion: **`demo_scalping_ws/` may not import** any of
`*_demo/execution_client`, `demo_scalping_exec/`, or `demo/ledger/`. The daemon's
read-only package can compute triggers but cannot reach a signed client or the
ledger writer directly — only `ws_bridge.py` (on the exec side) may. This keeps
the read-only boundary AST-verifiable, exactly as ROB-307 established.

### 3.2 Reuse map (do not reinvent)

| Concern | Reused component | New code |
|---|---|---|
| Signal logic | `demo_scalping/signal.py` (`evaluate_signal`) | event-driven adapter only |
| Risk contract | `demo_scalping/contract.py` (`ScalpingRiskLimits`, `evaluate_risk`, `ReasonCode`) | none |
| Ledger state | `demo_scalping/ledger_state.py` (`LedgerSnapshot`) | none |
| Execution | `demo_scalping_exec/executor.py` (`DemoScalpingExecutor`) | `ws_bridge.py` caller |
| Ledger writes | `demo/ledger/service.py` (`BinanceDemoLedgerService`) | none |
| Analytics | `demo_scalping_exec/analytics.py` (`ScalpTradeAnalyticsService`) | none |
| Daily review | `scalping_reviews` + `ScalpingReviewService` (ROB-315) | none |
| Public WS | `ws_client.py` (`BinancePublicWSClient`, ROB-285) | futures host + aggTrade parser |

---

## 4. Streams, symbols, and the market-data feed

**Symbols (conservative, demo-only, unchanged from ROB-307 §5):** `XRPUSDT`,
`DOGEUSDT`, `SOLUSDT`. `BTCUSDT` excluded (MIN_NOTIONAL > 10 USDT cap). The
existing `ScalpingRiskLimits.allowlist`/`excluded` remain authoritative; the
daemon does not introduce a second symbol source.

**Streams per symbol:**

* `@kline_1m` — closed-candle state. **This is the signal driver:** the trigger
  fires when a 1m candle closes, reusing the existing candle-based
  `evaluate_signal` (SMA + breakout) verbatim. Reacting at candle close (vs. up
  to 5 min later) is the improvement over polling — not sub-second ticks.
* `@bookTicker` — best bid/ask. Feeds the **spread guard** and the **required
  quote-freshness gate** (a trigger is emitted only with a fresh bid/ask).
* `@aggTrade` — trade prints; **momentum/liveness context only**. It does NOT
  drive the current signal and never substitutes for a bookTicker quote.

> Note: the current signal is **closed-1m-kline based**, not tick-level. A true
> tick-level signal would consume `aggTrade`/in-progress klines, but the
> ROB-285 `BinancePublicWSClient` emits **closed** klines only (`x: True`), and
> redefining the strategy is out of scope (issue: "reuse existing signal logic
> where safe"). A **futures aggTrade parser** is added for freshness/liveness
> (the existing client parses kline + bookTicker only).

---

## 5. In-memory state & data-freshness model

`state.py` holds a per-symbol `MarketState`: latest bid/ask/qty, latest trade
price/qty, optional latest closed kline, and a **`last_event_at` timestamp per
stream**.

**"Connection alive ≠ data fresh."** A silently half-dead socket can keep the
connection object open while delivering nothing. Freshness is therefore measured
from **last event received**, not from connection state:

* Trigger evaluation blocks with `ReasonCode.STALE_DATA` when
  `now - last_event_at > max_data_age_seconds` (existing 120s = 2×1m candle).
* The heartbeat (§8) surfaces per-symbol/per-stream staleness for external
  liveness checks.
* Reconnect/backoff (§6) repairs the socket; freshness guard protects trades in
  the meantime.

---

## 6. Trigger, concurrency, and reconnect model

### 6.1 Event-driven trigger

The supervisor runs one consumer task per symbol over a per-symbol event queue.
On each relevant event it: updates `MarketState`, checks freshness, then
evaluates the (pure, deterministic) trigger. No fixed timer. Repeated triggers
within a per-symbol **debounce** window are suppressed to absorb event bursts.

### 6.2 Concurrency — two-layer guard (closes the async race)

The DB ledger reflects only *committed* state. Between "submit entry" and
"ledger reconcile," an event-driven daemon could fire a second entry for the
same symbol — a race the sequential 5-minute tick never had. Defense:

1. **In-process asyncio guards (new):** a per-symbol lock + a global semaphore
   with capacity matching `global_open_lifecycle_cap` (1). The symbol is locked
   **before** the bridge call and released only after reconcile. This covers the
   in-flight window the DB cannot see yet.
2. **DB ledger re-check (existing):** `ws_bridge.py` re-loads the live
   `LedgerSnapshot` and re-runs `evaluate_risk` immediately before any executor
   call — the durable backstop, unchanged from ROB-307.
3. **Debounce (new):** per-symbol minimum interval between triggers.

Trigger evaluation stays sync/pure; only the bridge call is awaited and
serialized through the guards.

### 6.3 Reconnect / backoff / heartbeat

`market_stream.py` reconnects with exponential backoff + jitter on socket
drop/error and re-subscribes the symbol set. A heartbeat records last successful
event time per stream. If reconnect/backoff is not fully implemented in an early
slice, it ships as an explicit stub with `TODO` + a test asserting the stub's
contract (per acceptance criteria).

---

## 7. Safety gates & environment (3-layer)

```text
BINANCE_DEMO_SCALPING_ENABLED=false        # existing master capability (signal/exec/scheduler)
BINANCE_DEMO_SCALPING_WS_ENABLED=false     # NEW: long-running WS daemon gate (default false)
BINANCE_DEMO_SCALPING_WS_CONFIRM=false     # NEW: real Demo order-mutation gate (default false)
```

* **All three true** is required for any broker mutation.
* The daemon does **not** reuse the scheduler's confirm flag
  (`BINANCE_DEMO_SCALPING_SCHEDULER_CONFIRM`). Daemon and scheduler are
  independently gated so enabling one never silently enables the other.
* Signed credentials resolve via the existing `BINANCE_FUTURES_DEMO_*` /
  canonical `BINANCE_DEMO_*` chain (ROB-302). The daemon reads no new secrets and
  prints none.

**Gate behavior:**

| State | Subscribe? | Trigger/risk runs? | Broker mutation? |
|---|---|---|---|
| `WS_ENABLED=false` (or master off) | No | No | No |
| `WS_ENABLED=true`, `WS_CONFIRM=false` | Yes | Yes (preview/dry-run) | **No** — executor dry-run only |
| all three true | Yes | Yes | Yes (`demo-fapi`, demo-only) |

**Retained risk contract (unchanged):** symbol allowlist + `BTCUSDT` exclusion,
max notional 10 USDT, daily order cap 10, daily loss budget 5 USDT, cooldown
300s, spread guard 20bps, data-freshness guard, one-open-lifecycle/global cap.
Risk-blocked paths log the accumulated `ReasonCode`s and place no order.

**Fail-closed** when credentials, stream host, DB, or ledger state is
unavailable: the daemon refuses to trigger rather than trading blind.

---

## 8. Operational integration (inert in the code PR)

* **CLI:** `python -m scripts.binance_demo_scalping_ws_daemon` — default-disabled;
  with all gates off it parses config, reports disabled, and exits without
  subscribing.
* **launchd plist:** a **template** lives in this runbook (§11.7) only. It is
  **not** loaded/unloaded/enabled by tests or migrations. launchd is a local-Mac
  dev convenience; the **production** runtime target is a Linux container /
  systemd unit (this repo deploys via Docker — see `DEPLOYMENT.md`), so ops
  activation is not left hanging on a macOS-only artifact.
* **Health/heartbeat:** `health.py` emits a JSON snapshot — per-symbol freshness,
  connection state, last trigger reason, last outcome — for Hermes/Prefect to
  poll for liveness.
* **Structured logs:** symbol, trigger reason, risk reason code, outcome. No
  secrets.

**Prefect's role going forward** (it is **not** the hot-path trigger): daemon
health checks, reconciliation sweeps, daily review draft generation,
failure-only notifications, operator reports. The existing 5-minute deployment
is **not removed** here; an ops note recommends renaming/treating it as polling
intraday and pausing it only after the daemon is validated.

---

## 9. Non-goals / prohibited side effects

* No Binance live trading, no live endpoint routing, no real/live orders.
* No production `confirm=true` daemon run as part of tests.
* No editing or printing of secrets/credentials.
* No cron-based recurrence; no TaskIQ scheduling of the hot-path daemon.
* No deletion/rewrite of existing ledger/analytics/review tables.
* No marking related Linear issues Done; no mutating production
  scheduler/launchd/Prefect state without explicit operator approval.

---

## 10. Testing plan (fakes only — no network, no real orders)

Fake stream client + fake execution/risk client. Required coverage:

* websocket event → state update → signal trigger path;
* disabled gate blocks all subscription and execution;
* `confirm=false` blocks all broker mutation;
* stale stream/data guard blocks the trigger;
* risk-gate-blocked path logs reason codes, places no order;
* one-open-lifecycle / duplicate-trigger guard, **including the in-flight race**
  (second event during an unreconciled entry);
* reconnect/backoff or stream-failure handling;
* CLI config parsing + default-disabled behavior;
* **new:** `PUBLIC_FUTURES_STREAM_HOSTS` is read-only and the futures-demo signed
  transport still rejects `fstream.binance.com`.

---

## 11. Runbook

### 11.1 Preconditions
- `BINANCE_FUTURES_DEMO_*` (or canonical `BINANCE_DEMO_*`) credentials present in
  the operator env (never committed).
- DB reachable; `binance_demo_order_ledger` migrated.

### 11.2 Default-disabled startup (safe smoke)
```bash
uv run python -m scripts.binance_demo_scalping_ws_daemon
# All gates off -> prints "disabled", subscribes to nothing, exits 0.
```

### 11.3 Dry-run (stream + trigger + risk, NO orders)
```bash
BINANCE_DEMO_SCALPING_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_CONFIRM=false \
uv run python -m scripts.binance_demo_scalping_ws_daemon
# Subscribes fstream (read-only), evaluates triggers, runs risk + executor preview.
# Broker mutation is never reached. Watch structured logs for trigger/risk reasons.
```

### 11.4 Confirm-gated Demo startup (real demo-fapi orders)
```bash
BINANCE_DEMO_SCALPING_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_ENABLED=true \
BINANCE_DEMO_SCALPING_WS_CONFIRM=true \
uv run python -m scripts.binance_demo_scalping_ws_daemon
# Places real DEMO orders on demo-fapi.binance.com only. Still subject to every
# risk gate. Verify via the ledger after the first round-trip.
```

### 11.5 Health check
```bash
# Poll the health snapshot (path/transport finalized in the code slice).
# Confirm: connection=connected, each symbol fresh (age < 120s), last outcome sane.
```

### 11.6 Stop / rollback
- Stop the process (Ctrl-C / `launchctl unload` / container stop).
- To disable without stopping config plumbing: set `BINANCE_DEMO_SCALPING_WS_ENABLED=false` and restart.
- Open positions: reconcile via the existing demo futures reconcile path / smoke CLI; the daemon does not leave positions it cannot account for (one-open-lifecycle + reconcile gate).
- The 5-minute Prefect tick is unaffected and remains the fallback observation path.

### 11.7 launchd template (dev only — do NOT auto-load)
```xml
<!-- ~/Library/LaunchAgents/dev.robin.binance-demo-scalping-ws.plist (template) -->
<!-- Operator loads manually; never loaded/enabled by tests or migrations. -->
<!-- Prod runtime target is a Linux container/systemd unit, not launchd. -->
```
(Full plist finalized with the code slice; this section is the placement note.)

### 11.8 Failure categories
| Category | Symptom | Daemon behavior |
|---|---|---|
| Stream disconnect | socket drop/error | reconnect w/ backoff; freshness guard blocks trades until fresh |
| Stale data | events stop, socket "open" | `STALE_DATA` blocks trigger; heartbeat flags it |
| Risk block | cap/cooldown/spread hit | log reason codes, no order |
| Credential/DB/ledger unavailable | startup or runtime | fail closed, refuse to trade |
| Confirm off | dry-run | preview only, no mutation |

---

## 12. Acceptance-criteria mapping

| Acceptance item | Where addressed |
|---|---|
| Polling-vs-scalping distinction + final architecture | §1 |
| Runbook: disabled/dry-run/confirm/health/stop/failure | §11 |
| 5-min tick documented as polling intraday/smoke | §1, §8 |
| Default-disabled WS entrypoint | §3, §7, §11.2 |
| Demo/testnet-safe streams for allowlisted symbols | §2, §4 |
| In-memory state + freshness/stale guards | §5 |
| Event-driven trigger -> risk/executor bridge | §3, §6 |
| confirm=false never mutates; disabled never subscribes | §7 |
| Risk-blocked logs reason codes, no order | §6.2, §7 |
| One-open-lifecycle / concurrency guard | §6.2 |
| Reconnect/backoff + heartbeat (or stubbed + tested) | §6.3 |
| Test list | §10 |

---

## 13. Slicing (per ROB-307 4-PR precedent)

1. **This doc** — design/runbook (Scope §1). *(current slice)*
2. Skeleton (`demo_scalping_ws/` modules) + CLI + new read-only allowlist +
   import-guard assertion + disabled-path tests.
3. Stream → state → trigger + freshness + reconnect/backoff (fake stream tests).
4. `ws_bridge.py` → live ledger risk re-check → `DemoScalpingExecutor` +
   two-layer concurrency guard + confirm gate + analytics/review wiring +
   dedicated `docs/runbooks/` entry.

---

## 14. Handoff checklist (final PR series)

Per ROB-317 "Verification / handoff": branch + PR URLs; files changed; tests run
+ results; migrations (none expected — reuses existing tables); exact env flags
for dry-run vs confirm; explicit statement that no live orders / no production
scheduler mutation / no secret logging occurred; recommended Hermes post-merge
checks (health poll + ledger spot-check after first confirmed round-trip).
