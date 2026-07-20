# Binance Demo Strategy Loop (ROB-993) — Runbook

**Scope.** Operator runbook for the strategy-pluggable Binance USD-M
Futures **Demo** execution loop: real-time 1m→4h bar aggregation (H1
semantics), a plugin strategy interface, a kill switch, and wiring into
the existing `BinanceFuturesDemoExecutionClient` (ROB-298). This is
strategy-agnostic infrastructure — the S3 signal-engine adapter
(ROB-980) is **not** wired here; the default plugin (`NullStrategy`)
never emits a signal.

This lane shares the `demo-fapi.binance.com` execution client, the
`binance_demo_order_ledger` table, and (in production) the Demo API
credentials with the ROB-298 smoke CLI (`scripts/binance_futures_demo_smoke.py`)
and the ROB-307/ROB-841/ROB-844 demo-scalping executor
(`app/jobs/binance_demo_scalping_runner.py`). See §5 for the shared-account
interference implications before running `--confirm` in an environment
where those are also active.

---

## 1. What this is (and is not)

* **Is**: a real-time bar aggregator + plugin strategy interface + kill
  switch + order round-trip wiring, all strategy-agnostic. Verified by
  an operator-run smoke CLI, not a scheduler.
* **Is not**: a running trading strategy. `NullStrategy` (the only
  plugin shipped in this PR) always returns `None` — `--once`/`--loop`
  with the default plugin never places an order. The S3 adapter
  (evaluating `bars_4h_multi_symbol -> Signal | None` against the real
  ROB-980 signal engine) is a separate, later commit.
* **Is not** a scheduler/TaskIQ/cron registration. The only entry point
  is `scripts/binance_demo_strategy_loop.py`, run manually. `--loop`
  polls forever in the foreground; the operator starts/stops it (e.g.
  inside a tmux/screen session they own) — there is no daemon,
  Prefect flow, or cron entry anywhere in this PR.

---

## 2. Architecture

```
1m klines (demo-fapi, public unsigned GET)
        │  bars.fetch_1m_minute_bars — drops the in-progress candle
        ▼
research.nautilus_scalping.rob974_features.build_complete_4h  (H1, reused verbatim)
        │  UTC-aligned, complete-only 4h buckets — a bucket missing any of
        │  its 240 constituent 1m rows is simply never emitted (no
        │  forward-fill; NO_SIGNAL == absence).
        ▼
StrategyPlugin.evaluate(bars_4h_multi_symbol, decision_ts) -> Signal | None
        │  (NullStrategy in this PR; S3 adapter is a later commit)
        ▼
kill_switch.evaluate_kill_switch  (max concurrent positions, consecutive SL/day)
        │  both gates re-read fresh from binance_demo_order_ledger every tick
        ▼
sizing.compute_futures_demo_order_qty + quantize_qty  (LOT_SIZE floor, MIN_NOTIONAL guard)
        ▼
execution.execute_signal_round_trip
        │  reserve_root_planned → position-mode/leverage checks → open MARKET
        │  → bounded fill-poll → reduceOnly MARKET close → reconcile
        │  (mirrors scripts/binance_futures_demo_smoke.py --confirm exactly)
        ▼
binance_demo_order_ledger (product="usdm_futures")  +  correlation_id  +  forecast_save
```

Package: `app/services/brokers/binance/demo_strategy_loop/`
(`bars.py`, `strategy.py`, `kill_switch.py`, `sizing.py`, `execution.py`,
`correlation.py`, `orchestrator.py`). CLI:
`scripts/binance_demo_strategy_loop.py`.

### Why the immediate round trip (not hold-to-TP/SL)

This PR's `execute_signal_round_trip` opens then immediately closes
with `reduceOnly` — the same shape the ROB-298 smoke CLI proves. A real
strategy (S3) will want to hold a position until TP/SL/max-hold; that
position-management logic is strategy-specific and belongs in the S3
adapter commit, not this infra PR. The immediate round trip is enough
to prove every layer (bar aggregation → kill switch → sizing →
execution → ledger → correlation_id → forecast) wires together
end-to-end, which is exactly the ROB-993 verification AC ("페이퍼
신호로 e2e 스모크 — 주문 1건 데모 왕복").

---

## 3. Env variables

| Variable | Required? | Default | Notes |
|---|---|---|---|
| `BINANCE_DEMO_STRATEGY_LOOP_ENABLED` | Yes (must be `true`) | unset → disabled | Master kill-switch for this CLI. Checked after argparse, before any HTTP/DB. |
| `BINANCE_FUTURES_DEMO_ENABLED` | Yes | unset → disabled | Required by the underlying `BinanceFuturesDemoExecutionClient` (ROB-298). |
| `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET` | Yes¹ | — | Canonical shared Demo credentials (Spot + Futures + this loop). |
| `BINANCE_FUTURES_DEMO_API_KEY` / `_API_SECRET` | No | — | Futures-only override; wins over canonical when set. |
| `BINANCE_FUTURES_DEMO_BASE_URL` | No | `https://demo-fapi.binance.com` | Any non-demo-fapi host is refused at the transport layer (`BinanceLiveHostBlocked`). |

¹ Same resolution order as ROB-298: `BINANCE_FUTURES_DEMO_API_*` → `BINANCE_DEMO_API_*` → fail closed.

**No new credential surface** — this loop reuses `BinanceFuturesDemoExecutionClient.from_env()`
unchanged.

---

## 4. CLI modes

```bash
uv run python -m scripts.binance_demo_strategy_loop --readiness
uv run python -m scripts.binance_demo_strategy_loop --once
uv run python -m scripts.binance_demo_strategy_loop --once --confirm
uv run python -m scripts.binance_demo_strategy_loop --loop --poll-interval-seconds 300
uv run python -m scripts.binance_demo_strategy_loop --paper-signal --confirm
```

| Mode | HTTP/DB | Purpose |
|---|---|---|
| (no flag, disabled) | none | Default-disabled clean exit (0). |
| `--readiness` | none | Env-only report; no credentials required. |
| `--once` | bars + (if signal) execution | Single tick. With `NullStrategy` this always ends in `no_signal` unless `--paper-signal` is also used. |
| `--loop` | continuous | Polls at `--poll-interval-seconds` (default 300s); only acts once per newly-closed 4h bar (in-memory guard — see §6 limitation). Foreground, operator-managed. |
| `--paper-signal` | bars skipped; execution if `--confirm` | Injects a canned `Signal` (`--paper-symbol`/`--paper-side`), bypassing bar-fetch + strategy. **This is the ROB-993 e2e smoke path.** |

Shared flags: `--symbols` (default `XRPUSDT,DOGEUSDT,SOLUSDT`), `--cap-usdt`
(default 10), `--leverage` (default 1, only 1 accepted), `--max-concurrent-positions`
(default 1), `--max-consecutive-sl` (default 2), `--confirm` (operator gate —
without it every mode is dry-run, zero broker mutation).

Every tick emits one JSON evidence line (`event: "strategy_loop_tick"`) with
`decision_ts`, `signal`, `blocked_reason`, `round_trip`, and forecast-save
status — grep-friendly, mirrors the ROB-298 smoke CLI's evidence convention.

**Exit codes**: `0` clean (disabled / no signal / dry-run / kill-switch
gated / reconciled round trip), `1` operator misconfiguration (missing
env/credentials), `2` runtime failure (broker/ledger anomaly raised
mid-lifecycle — investigate the ledger row before retrying).

---

## 5. Shared-account interference (read before `--confirm` in prod-adjacent envs)

The Demo API credentials (`BINANCE_DEMO_API_KEY`/`_SECRET`) and the
`demo-fapi.binance.com` account are **shared** across every consumer of
this repo's Futures Demo lane: this loop, the ROB-298 smoke CLI, and —
in production — the ROB-307/841/844 demo-scalping executor
(`BINANCE_DEMO_SCALPING_SCHEDULER_ENABLED`). The kill switch and
`reserve_root_planned` exposure-slot cap only see **this process's own
local database's** `binance_demo_order_ledger` rows — they do **not**
know about broker-side state opened by a different process against the
same account (e.g. a running production scalping bot's open position).

**Before running `--confirm` against any environment whose Demo
credentials are also used by a live automation (production, or any
shared dev credential set):**

1. Confirm no other consumer currently holds an open position/order on
   the symbol you're about to trade (`GET /fapi/v2/positionRisk`,
   `GET /fapi/v1/openOrders` via `--preflight`-style tooling, or the
   `binance_demo_ledger_status` MCP tool).
2. Prefer a dedicated/non-production credential pair for this loop's
   own smoke/dev runs when one is available.
3. If you must share credentials with a live automation, coordinate
   timing with whoever operates it — a concurrent round trip on the
   same symbol can misattribute a reduceOnly close to the wrong
   process's position.

This is the same caveat that already applies to running the ROB-298
smoke CLI's `--confirm` mode against a shared account; nothing new is
introduced by this loop beyond another consumer of the same shared
resource.

---

## 6. Known limitations (v1 / this PR)

* **`--loop`'s "already processed" guard is in-memory only.** A fresh
  process restart re-evaluates the current 4h bar once more. This is
  a real gap for a strategy that isn't naturally idempotent per bar —
  acceptable for this infra PR (`NullStrategy` is idempotent by
  construction — it never signals), but the S3 adapter PR should
  either persist the last-processed `decision_ts` (e.g. via ledger
  metadata) or design the strategy to be safely re-evaluated.
* **No position-hold / TP-SL monitoring.** See §2 "why the immediate
  round trip." A real strategy that wants to hold until TP/SL needs
  additional state-machine work in a later PR (the demo-scalping
  executor's `execute_monitored` is a reference implementation of that
  shape, but it is scalping-specific and not reused directly here).
* **Consecutive-stop-loss tracking requires `exit_reason` metadata.**
  `kill_switch.build_kill_switch_snapshot` reads
  `extra_metadata["exit_reason"]` on closed root rows. This PR's own
  round trip always writes `exit_reason: "immediate_close"` (never
  `"stop_loss"`) — the SL gate is exercised by the unit tests
  (`tests/services/brokers/binance/demo_strategy_loop/test_kill_switch.py`)
  but won't trip against this PR's own traffic until a strategy that
  actually books stop-losses (writing `exit_reason: "stop_loss"`) is
  wired in.

---

## 7. Verification performed for ROB-993

* Full unit-test coverage of `bars`, `strategy`, `kill_switch` (pure +
  DB-backed), `correlation`, `sizing`, and `orchestrator` control flow
  (`tests/services/brokers/binance/demo_strategy_loop/`), plus the CLI
  (`tests/scripts/test_binance_demo_strategy_loop.py`).
* `ruff check` / `ruff format --check` / `ty check` clean on all new
  files.
* Full `tests/services/brokers/binance/` + `tests/scripts/` suite
  (1183 tests) green after this change — no regression to the ROB-298
  execution client, ledger service, or existing demo-scalping suites.
* `test_no_internal_llm_imports` and `test_repository_import_boundary_enforced`
  (the two repo-wide AST guards this package's imports could trip)
  pass unchanged.
* e2e smoke (`--paper-signal --confirm`): see the PR description / ROB-993
  ticket for the run evidence — this loop's `execute_signal_round_trip`
  reuses the exact lifecycle the ROB-298 smoke CLI already proves live
  against `demo-fapi.binance.com`, driven here by an injected `Signal`
  instead of CLI args.
