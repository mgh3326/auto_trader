# ROB-383 Phase 3 — Shortlist Validation Design

**Design spec — Phase 3 (validation) + Phase 4 (strategy-pack v0 recommendation).**
Follow-up to the Phase 1–2 sieve (merged PR #1058). Branch: `rob-383-phase3`.

Date: 2026-05-31 · Status: design approved, proceeding to plan.

---

## 1. Goal

Validate 5 clean-room-ported signals from the frozen shortlist on real Binance
USD-M klines via the existing `validated_gate`, produce counts-only verdicts,
classify each as `demo_ready_candidate` / `shadow_candidate` / `research_candidate`
/ `reject`, and write the Binance Demo strategy-pack v0 recommendation (Phase 4).

**Candidates (5, family-diverse, faithful-portable — OHLCV bar-based):**

| candidate_id | family | native tf | clean-room signal |
|---|---|---|---|
| `freqtrade_supertrend` | atr_trail | 1h | Supertrend (ATR band, trend flip) |
| `freqtrade_bbrsi_naive` | mean_reversion | 5m | Bollinger lower + RSI oversold (long-only mean-revert) |
| `tv_squeeze_momentum` | volatility | 1h | TTM squeeze (BB inside Keltner) release + momentum sign |
| `tv_range_filter` | trend | 1h | smoothed range filter (trend-state) |
| `tv_chandelier_exit` | atr_trail | 1h | ATR trailing-stop (Chandelier) trend |

**Expected outcome (prior evidence):** ROB-342/353/382 were all negative on
short-horizon crypto. Expect mostly `reject`/`research_candidate`, perhaps 1–2
`shadow`, likely 0 `demo_ready`. A 0-`demo_ready` verdict is a valid success.

## 2. Clean-room boundary

Every indicator (ATR, RSI, Bollinger, Keltner, Supertrend, Range Filter,
Chandelier) is reimplemented **from scratch** from the public mathematical
definition. **No GPL/Pine source is copied.** Cards' `source_url`s point at the
concept; the implementation is original. This is exactly the clean-room path the
issue sanctions for GPL/unclear-license sources.

## 3. Architecture (SEAM 2, pure signals + bounded fetch)

New package `research/nautilus_scalping/external_strategy_sieve/validation/`:

```
validation/
├── __init__.py
├── indicators.py   # pure bar-based indicators: atr, rsi, bollinger, keltner, supertrend, range_filter, chandelier
├── signals.py      # 5 signal fns: Sequence[Bar] -> list[validated_gate.Trade] (round-trips)
├── baselines.py    # random-entry baseline Trades (turnover-matched) + breakout reuse
├── classify.py     # GateReport -> {demo_ready/shadow/research/reject} + reasons (pre-registered)
├── frozen_params.py# the ONE frozen param set per signal + config_hash (no sweep)
├── runner.py       # load bars (fetch if missing) -> run signals -> evaluate_gate -> classify -> counts-only JSON
└── tests/
    ├── test_indicators.py   # known-series indicator values
    ├── test_signals.py      # synthetic bars -> expected trades (uptrend->long, oscillation->mean-revert)
    ├── test_baselines.py
    └── test_classify.py     # verdict/metrics -> class mapping
```

All pure stdlib, `uv run --no-project`. Imports `families` (Bar, make_taker_trade),
`validated_gate` (Trade, evaluate_gate, metrics_at_fee, turnover_matched_random_baseline),
`pit_bars`, `pit_klines_fetcher`, `pit_universe`, `cost_model`, `frozen_config`,
`artifact_paths` from the nautilus_scalping top level (all on `sys.path` via the
rootdir conftest).

### Signal contract

Each signal: `fn(bars: Sequence[families.Bar], **frozen_params) -> list[Trade]`.
Round-trip semantics: a position opens on the entry rule and closes on the exit
rule (non-overlapping — must close before the next entry). Gross PnL = realized
close-to-close return × notional, recorded via `families.make_taker_trade(gross,
ts, notional)` so `cost_model` rescales to any fee. Long-only (bbrsi) or
long/short flip (supertrend, chandelier, range_filter, squeeze).

### No parameter sweep (pre-registration)

The issue forbids hyperopt/sweep/tuning after seeing results. Each signal is
validated with **one frozen param set** (canonical indicator defaults, committed
in `frozen_params.py` with a `config_hash`). `evaluate_gate` is therefore called
with `candidate_runs={"default": trades}` — no val-best selection across a grid,
no island risk. Params are documented in the runbook before the run.

## 4. Validation methodology

- **Data:** `pit_bars.load_bars(symbol, interval, manifest)` over cached klines;
  missing months fetched via `pit_klines_fetcher.fetch_months(symbol, interval,
  from_month, to_month)` (skip-if-exists, gitignored `pit_data_root()/klines/`).
  Manifest: `PITManifest.load("data_manifests/pit_universe.v1.json").strict_usdt_perp()`.
- **Symbol panel:** BTCUSDT, ETHUSDT, XRPUSDT, SOLUSDT, DOGEUSDT (liquid). Trades
  pooled across symbols per signal for statistical power.
- **Window:** bounded ~2 years (operator-set `--from-month`/`--to-month`).
- **Costs:** trades recorded at `cost_model.REF_FEE_BPS=10`/leg; gate evaluated at
  the **Binance Demo taker `fee_bps=4.0`** (`frozen_config.taker_bps`); fee
  sensitivity reported across the grid `(10, 7.5, 5, 2, 0)` via `metrics_at_fee`.
- **OOS:** `evaluate_gate` walk-forward 50/25/25 (train/val/oos); verdict on the
  OOS fold.
- **Baselines:** `baseline_breakout` = `families.breakout_continuation_trades` on
  the same bars; `baseline_random` = turnover-matched random-entry Trades. Both
  required by `evaluate_gate`.
- **Metrics (counts-only):** gross/net PnL & bps, trade count, win rate, profit
  factor, expectancy, max drawdown, OOS net, verdict + reasons.

## 5. Classification mapping (pre-registered, in `classify.py`)

Run `evaluate_gate(..., fee_bps=4.0, min_trades=100)` per signal; map its
`GateReport`:

| GateReport.verdict | extra condition | class |
|---|---|---|
| `insufficient_data` (a fold < min_trades) | — | `research_candidate` (underpowered / data-gap) |
| `not_validated` | `results.gross.net_pnl <= 0` OR `net_after_cost <= 0` | `reject` |
| `not_validated` | gross-positive but failed OOS/baseline/stability | `research_candidate` |
| `validated` | net-positive OOS @ 4 bps, beats baselines, stable | `shadow_candidate` |
| `validated` | AND mean net bps/trade ≥ `economic_triviality_floor_bps` (0.5) AND positive across all 3 folds | `demo_ready_candidate` |

A candidate that cannot be faithfully validated (e.g., needs orderbook data the
panel lacks) is recorded `research_candidate` with reason `data_gap`, never
force-ported. Any simplification stamps `non_faithful_clean_room_spec` /
`horizon_changed_during_port` with reason.

## 6. Runner & output

- **Operator CLI:** `runner.py` default = dry-run (prints plan: symbols, intervals,
  frozen params, fee grid; no network). `--run` performs the bounded fetch + gate +
  classification. Lazy-imports Settings-free (research-only; no `app`).
- **Output:** counts-only JSON to `resolve_artifact_path('discovery', 'rob383',
  'phase3_validation.json')` (gitignored). No raw klines / dumps committed.
- **Report:** a Phase-3 verdict section appended to
  `docs/runbooks/external-crypto-strategy-sieve.md` (or a sibling
  `external-crypto-strategy-sieve-phase3.md`) — counts-only, per-candidate class +
  reasons, frozen-params record, and the Phase 4 strategy-pack v0 recommendation
  (0–2 demo_ready, 1–3 shadow, reject list, daily-retrospective fields).

## 7. Tests (pure, no network, no committed data)

- `test_indicators.py` — indicators on known short series (e.g., ATR of a fixed
  OHLC sequence equals the hand-computed value; RSI bounds).
- `test_signals.py` — synthetic bars: a monotonic uptrend yields a Supertrend long
  round-trip; a clean oscillation yields ≥1 bbrsi long round-trip; a flat series
  yields no trades. Determinism.
- `test_baselines.py` — random baseline is turnover-matched (same count) and seeded
  (deterministic).
- `test_classify.py` — each GateReport verdict/metrics shape maps to the expected
  class; data_gap → research_candidate; gross-negative not_validated → reject.

## 8. Safety boundaries (unchanged from Phase 1–2)

No live/Demo orders, no broker/order/watch/order-intent/trade-journal mutation, no
scheduler/TaskIQ/Prefect/cron/launchd, no prod DB/env/secret, no secret/raw-dump
commit. Klines fetch is read-only public `data.binance.vision` (same source the
existing campaigns use), cached gitignored. No external strategy runtime imported;
indicators are clean-room. No Demo activation issue opened automatically;
`demo_ready` (if any) only *recommends* a separate operator-approved issue.

## 9. Out of scope

SEAM 1 (Nautilus tick) — `nautilus_trader` is absent from the research venv.
Orderbook/L2 strategies (the shortlist's Hummingbot market-making entries are not
in this 5) — would be `research_candidate` (data_gap). Demo activation/scheduler.
