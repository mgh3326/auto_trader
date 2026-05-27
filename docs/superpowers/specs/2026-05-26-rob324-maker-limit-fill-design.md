# ROB-324 — Maker/limit-fill scalping edge re-evaluation (design)

- **Issue:** ROB-324 (follow-up to ROB-320 / PR #968)
- **Date:** 2026-05-26
- **Scope:** research/backtest only. No live trading, no Demo `confirm=true`, no
  broker/order/watch/order-intent mutation, no scheduler/Prefect/launchd, no prod
  DB/env/secret changes, no runtime parameter application, no `/invest` surfacing.
- **Location:** everything lives under `research/nautilus_scalping/` in its isolated venv.

## 0. Design revision (2026-05-27, approved)

The original §3 design rested the take-profit as a Nautilus LIMIT order. In testing,
a resting TP limit **under-filled** vs the taker's touch-based exit (`tp_hit` ~17% vs
the taker's ~51% win rate): positions rode to SL or end-of-data, collapsing maker
trade count to `insufficient_data` — a backtest matching-engine artifact, not economics.
Approved pivot to a cleaner, equivalent-fidelity model:

- **Entry** stays a passive, data-derived Nautilus limit, now posted `entry_offset_bps`
  (default 5) **below** the close (BUY) / above (SELL). Immediate reversions are **real
  missed fills** (~45% observed); continued moves fill → genuine entry-side adverse
  selection from real ticks.
- **Exit** is **touch-based**, identical trigger logic to the taker (SL-first), so the
  maker trade set is comparable to taker (~284 vs 466 for one symbol/config) instead of
  collapsing. `tp_hit` returns to ~47%.
- **Fees** are applied analytically, not by Nautilus: the maker re-sim runs on a
  **zero-fee instrument** (so `realized_pnl` = pure gross price P&L) and `maker_fill`
  charges the **real per-leg fees** — maker 2 bps on the entry + the TP leg, taker 4 bps
  on the SL leg — plus the conservative overlay. This is still a re-sim with real fills,
  not a fee-only rescale (it satisfies the commission-artifact note).
- Because there is no longer a resting exit limit, the venue reverts to ROB-320's
  `OmsType.HEDGING` + USDT-only funding for both modes (every exit is an explicit market
  `close_all_positions`, OMS-agnostic; taker reproduces ROB-320 exactly).

Sections below retain the original framing; where they describe a resting TP limit, read
the touch-based + analytic-fee model above.

## 1. Problem

ROB-320 produced an honest `not_validated` for the `meanrev_zscore_fade` candidate on
`XRPUSDT` + `BTCUSDT`: there is a small **gross** edge (whole-run PF ≈ 1.058) that
collapses once taker fees are applied (`net_after_cost` OOS ≈ −51.43 USDT, PF ≈ 0.37).

Two facts make a re-evaluation worthwhile:

1. **The fee assumption was doubly pessimistic.** ROB-320's catalog instrument and gate
   used **10 bps** per leg. The captured live commission artifact shows the real Binance
   **USDⓈ-M Futures Demo** schedule for both symbols is **maker 2.0 bps / taker 4.0 bps**
   (`research/nautilus_scalping/results/rob324/binance_usdm_commission_rates.json`).
2. **Both legs were taker.** Entry is `order_factory.market(...)`; exit is
   `close_all_positions(...)` on a tick TP/SL hit. A maker/limit execution model could
   pay less fee and capture price improvement.

**Question:** can a *conservative* maker/limit execution model recover a net-after-cost
edge without overclaiming validation?

**Honest prior:** even an optimistic maker rescale (2 bps, assuming every limit fills)
leaves the ROB-320 OOS fold ≈ −7 USDT. The likely outcome is another `not_validated`.
The deliverable is the honest model + auditable artifact, **not** a green light.

### Non-negotiable: no fee-only rescale
The commission artifact's own note (line 53) states: *"Use maker_fee_bps only with a real
maker/limit-fill model; fee-only rescale is not sufficient evidence for maker execution."*
Simply lowering `fee_bps` on the existing taker trade set would silently assume 100% fill
at favorable prices — exactly the overclaim to avoid. **Fills must be derived from real
price action.**

## 2. Approach (chosen)

Real tick-level limit-fill **re-simulation** in Nautilus, plus a pure deterministic
conservative overlay. Three scenarios are produced and fed to the **existing, unchanged**
`validated_gate.evaluate_gate`:

| # | Scenario | Production | Fees |
|---|----------|------------|------|
| 1 | **Taker baseline (realistic)** | Taker run, evaluated at **4 bps** both legs (corrects ROB-320's 10 bps). Single-rate, so the gate's existing linear rescale is valid. | 4 / 4 bps |
| 2 | **Maker/limit (data-derived)** | **New** Nautilus re-sim: LIMIT entry posted at the signal-bar close, filled against real trade ticks; if the next bar (60 s) of ticks never reaches it → cancel = **missed fill** (0 pnl, 0 fee). TP = maker limit (2 bps), SL = taker stop (4 bps). Entry maker = 2 bps. True per-leg fees baked into realized net. | maker 2 / taker 4 |
| 3 | **Conservative** | Pure overlay on Scenario 2 records: drop **25%** of "easy-TP" fills (TP hit with ~0 adverse excursion → front-of-queue fills you would not realistically win) **plus** a **1.0 bp** adverse-selection cost on every maker entry. | as #2 + haircut |

Baselines inside the gate (`micro_breakout`, `random_entry`) are evaluated at the same
**4 bps** taker so the candidate comparison is apples-to-apples.

### Alternatives rejected
- **Pure parametric overlay only** (no re-sim): fast and pure, but fills are assumptions,
  not price-derived — contradicts the artifact note's requirement for a real fill model.
- **Both side-by-side**: most auditable, but heaviest; deferred. The re-sim already gives
  the honest answer.

## 3. Components

All under `research/nautilus_scalping/`.

### 3.1 `strategy_meanrev.py` (extend)
Add `execution_mode: str = "taker"` to `MeanRevScalperConfig`.
- **Default `"taker"` preserves current behavior** — existing runs and parity tests
  (`test_meanrev_parity.py`, `test_signal_parity.py`) are unaffected.
- `"maker"` branch:
  - Entry: `order_factory.limit(...)` at the signal-bar close price (BUY at close, SELL
    mirror). Track submission bar.
  - Fill timeout: if not filled by the end of the next bar, cancel the working order =
    missed fill. Implemented via bar count since submission in `on_bar`.
  - Exit: TP as a resting maker `limit`; SL remains a taker stop/market (must get out).
    SL-first conservative ordering retained.
  - Record per-trade **adverse excursion** (max unfavorable price move between fill and
    exit, in bps) so the overlay can classify "easy-TP" fills.

### 3.2 `backtest_runner.py` (extend)
- When `execution_mode == "maker"`, build a **real-fee instrument override** (maker
  0.0002 / taker 0.0004) from the catalog instrument in-process — **no catalog rebuild**.
- Emit richer per-trade records for maker runs: `net_ref_pnl` (true realized net at real
  per-leg fees), `commission_ref` (true total commission magnitude), `notional`,
  `ts_opened`, `filled` (bool), `adverse_excursion_bps`, `tp_hit` (bool).
- Taker runs emit the existing 4-field record unchanged.

### 3.3 `maker_fill.py` (new, pure — no Nautilus import)
- `MakerTradeRecord` dataclass (the rich record above).
- `build_taker_baseline(taker_trades, fee_bps=4.0) -> list[Trade]` — rescale 10→4.
- `build_maker_optimistic(records) -> list[Trade]` — keep all fills; net already at real
  fees; `commission_ref` carries true commission so the gate's gross column still works.
- `build_maker_conservative(records, queue_loss_pct=0.25, adverse_bps=1.0,
  excursion_eps_bps=2.0) -> list[Trade]` — deterministically drop easy-TP fills (selection
  by a stable hash of `ts_opened`, so it is reproducible and not random) and subtract
  `adverse_bps * notional / 10_000` from each surviving maker entry.
- `classify_easy_tp(record, excursion_eps_bps=2.0) -> bool` — `True` when the trade hit TP
  and its `adverse_excursion_bps <= excursion_eps_bps` (i.e. price barely moved against the
  fill before reaching TP → a front-of-queue fill unlikely to be won in reality).
- All functions pure and unit-testable; they output plain `validated_gate.Trade` lists.

### 3.4 `validate_maker_fill.py` (new driver)
- Runs the taker meanrev + baselines (existing runner) and the **maker** meanrev re-sim,
  across `XRPUSDT` + `BTCUSDT`, merged chronologically (mirrors `validate_candidate.py`).
- **Reuses ROB-320's 2-config param grid** (`z2.0/tp30/sl30`, `z2.5/tp40/sl40`) for the
  taker and maker candidate scenarios, so the gate's param-stability / `param_island`
  overfit guard is preserved (a single config would make that check trivially pass).
- Builds the three scenarios via `maker_fill.py`.
- Calls the existing `evaluate_gate` once per scenario (per-label `candidate_runs`;
  baselines at 4 bps). The headline verdict is taken from the **conservative**
  scenario (the honest lower bound); the optimistic and taker results are reported for
  comparison.
- Writes the artifact (§4). No execution side effects — public-data backtest only.

### 3.5 Gate reuse — the one subtlety
`validated_gate.py` stays **byte-for-byte unchanged**. Maker scenarios cannot use the
single-rate fee rescale (mixed maker/taker legs break linearity). Convention:

- Each maker `Trade` carries the **true net at real per-leg fees** in `net_ref_pnl`
  (post-haircut for the conservative scenario) and the **true total commission magnitude**
  in `commission_ref`.
- The driver calls `evaluate_gate(..., fee_bps=REF_FEE_BPS)`. At the reference point
  `scale = 0`, so `net_after_cost = net_ref_pnl` (as-run), while the gross column
  (`fee_bps=0`, `scale=1`) adds `commission_ref` back. The driver only ever evaluates
  maker scenarios at the reference point and 0 — never intermediate fees — so the mixed-leg
  non-linearity is never exercised.
- `cost_model` in the artifact records the **real** maker/taker bps (not 10) to prevent
  confusion, with a note explaining the convention.

**Fallback** (only if review finds the convention too implicit): a minimal *additive,
non-verdict-affecting* gate parameter (e.g. `rescale=False`). Tried the convention first
to honor "gate unchanged".

## 4. Artifact

`research/nautilus_scalping/results/rob324/maker_fill.json`, `schema_version:
"validated_signal_gate.v2"`. Superset of the ROB-320 v1 report, adding:

- `fill_model`: `{entry_rule, fill_timeout_bars, tp_execution, sl_execution,
  queue_loss_pct, adverse_bps, excursion_eps_bps}`.
- `cost_model`: real `maker_fee_bps: 2.0`, `taker_fee_bps: 4.0`, and
  `commission_source: "results/rob324/binance_usdm_commission_rates.json"`.
- `scenarios`: for each of the three — symbols, window, OOS + per-fold metrics, baseline
  comparison, overfit flags, trade/fill/missed-fill counts.
- Top-level `verdict` (from the conservative scenario) + `verdict_reasons`.

The captured `binance_usdm_commission_rates.json` is copied into this branch under
`results/rob324/` so the fee provenance ships with the result.

## 5. Tests

- `tests/test_maker_fill.py` (new, pure):
  - **Missed-fill path:** an unfilled record contributes 0 pnl and 0 fee; missed-fill
    count is correct.
  - **Adverse-selection / queue-loss path:** conservative overlay drops the right easy-TP
    fills (deterministic) and applies the adverse cost; net is strictly worse than the
    optimistic scenario.
  - **Fee math:** maker net uses 2/4 bps; taker baseline rescales 10→4 correctly.
  - **Determinism:** the queue-loss selection is stable across runs (hash-based).
- Gate/report test asserting the produced verdict is always one of
  `validated` / `not_validated` / `insufficient_data`.
- Existing `test_validated_gate.py`, `test_meanrev_*`, `test_signal_parity.py` remain
  green (the `execution_mode` default is `"taker"`).

## 6. Execution prerequisites

- The rob-324 worktree has no venv/catalog (both gitignored). The **rob-320 worktree**
  has a built ParquetDataCatalog with trade-tick data for both symbols across the window.
- Plan: verify the rob-320 nautilus venv imports; run with `--catalog <rob-320 catalog>`
  (read-only). If the venv is broken, rebuilding the Intel-mac Rust nautilus is the costly
  fallback — surface before spending time on it.

## 7. Acceptance criteria (from the issue) — how this design meets each

- Pure tests cover maker/limit-fill assumptions + ≥1 adverse-selection/missed-fill path →
  `tests/test_maker_fill.py` (§5).
- Gate/report tests prove the verdict stays within the three-value vocabulary →
  gate/report test (§5); verdict logic is the unchanged `evaluate_gate`.
- Artifact records symbols, window, fee model, fill model, OOS metrics, baseline
  comparison, overfit flags → `maker_fill.json` (§4).
- PR handoff comment states artifact path, verdict, and side-effect boundary → produced at
  PR time.
- CI/lint passes for the touched research code → `ruff` on the research files + the pure
  test suite.

## 8. Safety boundary

Research only. Public trade-tick data, isolated venv, read-only catalog access. No live
trading, no Demo `confirm=true`, no broker/order/watch/order-intent mutation, no
scheduler/Prefect/launchd, no prod DB/env/secrets, no runtime parameter application, no
`/invest` surfacing. The commission artifact is read-only input and contains no secrets,
signatures, balances, positions, or account identifiers.
