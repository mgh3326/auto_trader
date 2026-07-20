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

Shared flags: `--symbols` (default `XRPUSDT,DOGEUSDT,SOLUSDT`), `--leverage`
(default 1, only 1 accepted), `--confirm` (operator gate — without it every
mode is dry-run, zero broker mutation).

**No CLI flag exists for leg notional, max concurrent positions, or
consecutive-SL limit.** Per the ROB-993 adversarial review
(`verify-993-2256.md`, Finding 1), those are hard lane invariants — leg
notional locked to `[6, 10]` USDT (`sizing.LEG_NOTIONAL_CAP_MIN_USDT`/
`_MAX_USDT`), max concurrent positions locked to `1`, consecutive-SL cap
locked to `2` (`kill_switch.LOCKED_LIMITS`) — not operator-tunable dials.
`orchestrator.run_tick` also asserts this itself (raises
`LegNotionalCapNotLocked`/`KillSwitchLimitsNotLocked` before any
network/DB call) so any future caller that bypasses the CLI is protected
too.

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
same account.

**Code-level defense (added in the ROB-993 adversarial-review hardening
pass — see §8):** `execute_signal_round_trip` now runs a fresh signed
`get_position`/`get_open_orders` snapshot immediately after the root
reservation and refuses the entire round trip (`RoundTripBlocked`, zero
submits) if the target symbol is not flat with zero open orders. The
close leg's quantity is computed from the position **delta** attributable
to our own open fill, not the raw account-wide `positionAmt` — a
mismatched delta (another consumer traded the same symbol in the narrow
window between the gate and our own fill) aborts before any close
submit. This does not make concurrent use fully safe (the gate has its
own narrow TOCTOU window between the snapshot and our own submit), but it
converts "silently mis-close someone else's position" into "fail closed
with an anomaly row" for the common case.

**Still recommended before running `--confirm` against any environment
whose Demo credentials are also used by a live automation:**

1. Confirm no other consumer currently holds an open position/order on
   the symbol you're about to trade (`GET /fapi/v2/positionRisk`,
   `GET /fapi/v1/openOrders` via `--preflight`-style tooling, or the
   `binance_demo_ledger_status` MCP tool) — belt-and-suspenders on top of
   the code-level gate above.
2. Prefer a dedicated/non-production credential pair for this loop's
   own smoke/dev runs when one is available.
3. If you must share credentials with a live automation, coordinate
   timing with whoever operates it.

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
  instead of CLI args. Re-run clean after §8's hardening pass.

---

## 8. Adversarial-review hardening (R2, verify-993-2256.md)

An independent adversarial review of the initial PR found 5 P1 safety
gaps, all fixed (TDD — a regression test reproducing each gap was written
and confirmed to fail against the pre-fix code before the fix landed):

1. **Hard invariants, not CLI dials.** Leg notional (`[6, 10]` USDT), max
   concurrent positions (`1`), and consecutive-SL cap (`2`) are now locked
   constants (`sizing.LEG_NOTIONAL_CAP_MIN_USDT`/`_MAX_USDT`,
   `kill_switch.LOCKED_LIMITS`) with no CLI flag to override them.
   `orchestrator.run_tick` also asserts this itself
   (`LegNotionalCapNotLocked`/`KillSwitchLimitsNotLocked`) before any
   network/DB call. `execute_signal_round_trip`'s `global_open_root_cap`
   parameter was removed entirely (hardcoded to `1` internally) rather
   than left as an overridable default.
2. **Broker-flat pre-submit gate + own-fill-attributed close qty.** See
   §5. `execute_signal_round_trip` now checks `get_position`/
   `get_open_orders` immediately after reservation, before any other
   broker call, and computes the close quantity from the position delta
   attributable to its own fill rather than trusting the raw account-wide
   `positionAmt`.
3. **Root exposure slot held blocking until reconcile completes.** The
   open root no longer transitions to the non-blocking `closed` lifecycle
   state until AFTER the open_orders-empty / position-flat / close-fill-
   proven checks have all passed; a reconcile failure now records
   `anomaly` directly from `filled` (still blocking) instead of releasing
   the slot first. See `execution._reconcile`.
4. **Broker-echo verification.** Every submit/poll response trusted as
   order-shape or fill evidence (open submit, close submit, and both
   fill-proof polls) is now compared against what was requested —
   symbol/side/client_order_id/qty/reduceOnly — via
   `execution._assert_order_echo`; any mismatch raises
   `BrokerEchoMismatch` and records an anomaly rather than accepting a
   tampered/inconsistent response.
5. **Synchronized multi-symbol decision bucket.** `run_tick` now requires
   every symbol in the universe to have a complete 4h bar ending at the
   exact same `close_ts` before invoking the strategy (`blocked_reason=
   "missing_complete_4h_bar"` otherwise) — previously a bare `max()`
   across symbols' latest bars could hand the plugin a snapshot where a
   lagging symbol's bar was stale, silently violating H1's synchronized-
   plane semantics.

New regression coverage: `tests/services/brokers/binance/demo_strategy_loop/test_execution.py`
(controllable fake execution client + ledger in `_fakes.py`, used to
reproduce deliberately mutated/tampered broker responses deterministically
without any real HTTP/DB) plus additional cases in `test_orchestrator.py`.
Re-verified against the real `demo-fapi.binance.com` Demo API after the
fix (clean reconciled round trip, same as §7).
