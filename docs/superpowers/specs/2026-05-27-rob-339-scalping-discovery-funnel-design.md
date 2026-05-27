# ROB-339 — Scalping strategy discovery funnel + fast-screen harness (design)

- **Issue:** ROB-339 (follow-up to ROB-320 / PR #968 and ROB-324 / PR #984, both `not_validated`)
- **Date:** 2026-05-27
- **Scope:** research/backtest only. No live trading, no Demo `confirm=true`, no
  broker/order/watch/order-intent mutation, no scheduler/Prefect/launchd, no prod
  DB/env/secret changes, no runtime parameter application, no `/invest` surfacing.
- **Location:** everything under `research/nautilus_scalping/` in its isolated venv,
  plus this design note. Public trade-tick parquet catalog is read-only.
- **Companion gstack design doc:** `~/.gstack/projects/mgh3326-auto_trader/mgh3326-rob-339-design-20260527-170544.md` (decisions D1–D6).

## 1. Problem

ROB-320 and ROB-324 both returned an honest `not_validated` for
`meanrev_zscore_fade`: a small **gross** edge (whole-run PF ≈ 1.058) that dies on
fees. ROB-324's maker/limit re-eval, even optimistic (maker 2 bps), lands OOS net
≈ −1.95 / PF 0.95; conservative ≈ −3.69 / PF 0.90, with 1,150 missed fills of
2,318 attempts. The dominant finding across ROB-316/320/324 is that **fees kill
the gross edge** — nothing tested so far clears the ~6–8 bps round-trip budget.

The full conservative gate is **expensive**: it replays 75 days of trade-tick data
for `BTCUSDT.BINANCE` + `XRPUSDT.BINANCE` across multiple Nautilus subprocess runs.
The open question is no longer "is the fee too high" but "**does any scalping
condition have enough gross edge to survive realistic fees, missed fills, and
adverse selection?**" — and we want to answer that cheaply *before* paying for
full validation.

**Goal:** a research-first **discovery → pilot → full-validation funnel** that
screens hypothesis families fast and emits an auditable, explicitly *non-canonical*
recommendation for which families deserve full validation next.

**Honest prior:** given ROB-316/320/324, the likely outcome is that most or all
families screen out on fees. The deliverable is the honest funnel + artifact, **not**
a green light. Discovery never produces `validated`.

## 2. The funnel

| Stage | Cost | Engine | Produces | Verdict vocabulary |
|-------|------|--------|----------|--------------------|
| **Discovery** (this PR) | seconds | none — pandas/pyarrow over catalog parquet | feature→outcome summaries per hypothesis | `screened_out` / `needs_more_data` / `promote_to_full_validation` (non-canonical) |
| **Pilot** (PR2+) | minutes | Nautilus, reduced grid + bounded window | a strategy run on a short window | non-canonical (still discovery-grade) |
| **Full validation** (existing) | tens of minutes | Nautilus, 75-day, full grid + baselines + bootstrap CI | `validated_signal_gate.v1` report | `validated` / `not_validated` / `insufficient_data` |

**What discovery screens cheaply:** does a condition's expected gross move clear
the fee+slippage budget, with enough samples, on a held-out tail. **What still
needs full validation:** walk-forward OOS, baseline beat (`micro_breakout`,
`random_entry`), overfit flags, bootstrap CI / MC permutation, real maker fills.
Discovery's job is to *reject cheaply* and *nominate*, never to bless.

## 3. Decisions (from the gstack design doc)

- **D1** — ROB-324 is a *reference* dependency, not a code dependency. PR #984
  merged to main first (squash `a76f960`); this branch is cut fresh from origin/main.
  No `maker_fill` / Nautilus import in the discovery harness.
- **D2** — `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`, opt-in; unset → existing repo
  `results/`. Read via research-local `paths.py` (`os.environ`), **never** app
  `Settings`/pydantic. Namespace separation: `discovery/` vs `gate/`.
- **D3** — discovery reads the same Nautilus `ParquetDataCatalog` parquet via
  pandas/pyarrow directly (no engine boot); not raw aggTrades.
- **D4** — discovery's `--window-from/--window-to` is a **real** constraint via
  pyarrow predicate pushdown. The Nautilus `backtest_runner` window is PR2.
- **D5** — multiple-hypothesis defense: record `hypotheses_tested`; label promote
  candidates `in_sample_only: true`; time-ordered OOS holdout inside discovery.
- **D6** — PR slicing (this doc is PR1's design note); see §8.

## 4. Approach (chosen) — pure-parquet discovery harness

A new `research/nautilus_scalping/discovery/` package that, for a given catalog,
symbol set, and window:

1. **Loads** trade ticks from catalog parquet via pyarrow with a `[from, to)`
   predicate pushdown (D4), aggregates to 1-minute OHLCV bars matching the
   strategies' `1-MINUTE-LAST` bar type.
2. **Features** per bar (bounded, vectorized): 1/3/5m returns, range, volume,
   rolling realized-vol bucket (low/normal/high), close position within bar range,
   rolling N-bar high/low (for sweeps), KST/EU/US/funding-neighborhood time bucket.
3. **Hypotheses** (the issue's five families) each define a boolean condition over
   features and a forward outcome (1/3/5m forward return, bps):
   1. **Momentum continuation** — recent extension + volume/range expansion + close
      near extreme → forward continuation.
   2. **Liquidity sweep / fake-breakout reversal** — N-bar high/low swept + wick
      re-entry + volume spike → forward reversal.
   3. **Volatility regime filter** — segment outcomes by realized-vol bucket; reject
      regimes where expected move can't clear fees.
   4. **Time-of-day filter** — segment outcomes by session bucket.
   5. **Maker/passive-entry viability** — estimate **missed-fill ratio**: fraction
      of signal bars where a limit posted `entry_offset_bps` from close would *not*
      fill within the timeout (from real ticks); treat missed-fill as a signal-quality
      signal, not only an execution cost.
4. **Summaries** per hypothesis: `sample_count`, `gross_expectancy_bps` (mean
   forward outcome), `fee_adjusted_bps` (gross − round-trip fee budget), regime/time
   bucket, `missed_fill_ratio` (maker), and an OOS-tail confirmation read.
5. **Classify** (§6) into the non-canonical recommendation.

**Fee budget** (from ROB-324's captured `binance_usdm_commission_rates.json`:
maker 2.0 / taker 4.0 bps): default screen uses the **realistic taker round-trip
= 8 bps**; the maker-entry variant (maker 2 + taker 4 = 6 bps) is reported
alongside. Configurable via `--fee-budget-bps`.

### Alternatives rejected
- **Reuse `backtest_runner` subprocess per hypothesis** — not actually fast
  (engine boot + Rust logger global singleton per run); defeats "screen before
  paying full cost". This is the pilot stage, not discovery.
- **Read raw `data.binance.vision` aggTrades** — would diverge from the catalog
  normalization that full validation uses; discovery and validation must share one
  data source.

## 5. Components (all under `research/nautilus_scalping/`)

### 5.1 `paths.py` (new, pure)
- `research_artifact_root() -> Path` — `AUTO_TRADER_RESEARCH_ARTIFACT_ROOT` if set,
  else the module-relative `results/`. Plain `os.environ`; no app `Settings` import.
- `resolve_artifact_path(namespace: str, *parts) -> Path` — `root / namespace / *parts`;
  `namespace ∈ {"discovery", "gate"}`. Single source of truth for artifact location.
- Creates parent dirs on write only; pure path resolution otherwise.

### 5.2 `discovery/data.py` (new, pure)
- `load_bars(catalog, symbol, window_from, window_to) -> DataFrame` — pyarrow
  predicate-pushdown read of catalog trade-tick parquet, aggregated to 1-min bars.
  The window is a **real row filter**, not metadata.

### 5.3 `discovery/features.py` (new, pure)
- Vectorized feature columns (§4.2). No Nautilus import.

### 5.4 `discovery/hypotheses.py` (new, pure)
- One callable per family producing a `HypothesisSummary` (condition description,
  sample_count, gross/fee-adjusted expectancy, bucket, missed_fill_ratio,
  oos_confirmation). Time-ordered split: in-sample head (75%) drives the decision;
  tail (25%) is a confirmation read only (D5c).

### 5.5 `discovery/screen.py` (new, pure)
- Fast-fail classifier (§6) → `Recommendation`.
- Artifact assembly (§7) including `hypotheses_tested` and `in_sample_only`.

### 5.6 `discover.py` (new driver, top-level under nautilus_scalping/)
- CLI: `--catalog --symbols --window-from --window-to --fee-budget-bps
  --min-samples --export`. Loads bars, runs all hypotheses, classifies, writes the
  artifact via `resolve_artifact_path("discovery", ...)`. No execution side effects.

## 6. Fast-fail classification (non-canonical)

Per hypothesis, in priority order:
- **`needs_more_data`** — `sample_count < min_samples` (default 200) in any
  decision fold.
- **`screened_out`** — any of: `fee_adjusted_bps <= 0`; gross move can't clear the
  fee budget; (maker) `missed_fill_ratio` above threshold (default 0.6); OOS-tail
  expectancy sign disagrees with in-sample.
- **`promote_to_full_validation`** — `fee_adjusted_bps > 0` with sufficient samples
  **and** OOS-tail sign agrees; always tagged `in_sample_only: true`.

These are **recommendations**, never gate verdicts. The artifact states this
explicitly. Final `validated` stays owned by the unchanged
`validated_gate.evaluate_gate` (OOS/baseline/overfit + bootstrap CI).

## 7. Artifact

`resolve_artifact_path("discovery", "<run-id>", "discovery.json")`,
`schema_version: "scalping_discovery.v1"`:
- `run`: symbols, window `{from, to}` (real, enforced), `fee_budget_bps`,
  `min_samples`, horizons, bucket definitions, catalog provenance.
- `hypotheses_tested`: integer count (D5 multiple-comparison transparency).
- `hypotheses[]`: each — `name`, `conditions` (human-readable predicate),
  `sample_count`, `gross_expectancy_bps`, `fee_adjusted_bps`, `regime`/`time_bucket`,
  `missed_fill_ratio` (maker only), `oos_confirmation`, `recommendation`
  (`screened_out`/`needs_more_data`/`promote_to_full_validation`), `reason`,
  `in_sample_only`.
- `note`: "Discovery output is non-canonical. Only the conservative gate
  (`validated_signal_gate.v1`) produces `validated`/`not_validated`."

## 8. PR slicing (D6)

- **PR1 (this design note + harness):** `paths.py`, `discovery/` package,
  `discover.py` driver, artifact schema + fast-fail classifier, discovery-path real
  window filter, unit tests (no 75-day tick data in CI). Optional: one bounded smoke
  run against the rob-320 catalog with elapsed time + artifact path in PR notes.
- **PR2:** `backtest_runner` real window constraint (catalog start/end query if the
  API supports it, else post-load `ts_event` filter + documented limitation with a
  test), baseline run caching keyed by catalog/window/symbol/params/code-version,
  vectorized fixed-point decode (PR1 uses a per-row Python decode — ~50s for a
  15-day 2-symbol window; numpy/int128 vectorization makes the full 75-day window
  "fast"), optional precise maker-viability re-sim borrowing ROB-324's `maker_fill`.

## 9. Tests (no full 75-day data; CI-safe)

- `tests/test_discovery_paths.py` — ENV set → root; unset → `results/`; namespace
  separation; no app `Settings` import.
- `tests/test_discovery_window.py` — synthetic small parquet; predicate pushdown
  excludes out-of-window rows (proves D4 is a real constraint).
- `tests/test_discovery_features.py` — tiny synthetic bar series → known feature
  values.
- `tests/test_discovery_screen.py` — synthetic summaries → correct
  `screened_out` / `needs_more_data` / `promote_to_full_validation`; `in_sample_only`
  always set on promote; artifact has `hypotheses_tested` and the non-canonical note.
- Existing `test_validated_gate*.py`, `test_meanrev_*`, `test_signal_parity.py`
  remain green (discovery adds no import into the gate or strategies).

## 10. Execution prerequisites + catalog encoding (verified at smoke)

- catalog/data/.venv are gitignored and **absent in a fresh worktree**. The
  **rob-320 worktree** has a built `ParquetDataCatalog` with trade ticks for both
  symbols. The discovery harness reads it via pandas/pyarrow with **no Nautilus
  import**, so it runs in the repo's own uv venv (pandas/pyarrow present) — no Rust
  nautilus rebuild needed for discovery.
- **Catalog encoding (verified):** this is a **128-bit high-precision** Nautilus
  catalog — `price`/`size` are `fixed_size_binary[16]` int128 little-endian, raw =
  `value * 10**16` (fixed by the build), and `ts_event` is `uint64` ns. The display
  precision in schema metadata (`price_precision`/`size_precision`) only rounds the
  decoded float; it is NOT the raw scale. `data._decode_int128_le` + `_decode_if_binary`
  handle this; plain-float parquet (the unit fixtures) is passed through unchanged.
- **CI requires none of this** — the unit tests use synthetic fixtures.

### Smoke results (XRPUSDT + BTCUSDT, 2026-03-01..03-15, ~50s, fee budget 8 bps)
All 10 (2 symbols × 5 families) → **`screened_out`**. In-sample gross expectancy
spans **−0.58 .. +0.44 bps** — none clears the 8 bps round-trip fee budget; OOS-tail
fee-adjusted is also negative throughout. This reproduces the dominant
ROB-316/320/324 finding: the gross edge does not survive fees.

**Recommended next family:** none of the five clears fees on this data, so **no
family is promoted to full validation** as-is. The least-negative gross was
**BTCUSDT/sweep_reversal (+0.44 bps)** and **XRPUSDT sweep/time-of-day (+0.28 bps)** —
if any direction is worth a deeper look it is **liquidity-sweep reversal**, but only
after a maker/limit cost model (entry ~2 bps) and tighter regime/time segmentation
materially lift gross expectancy; at taker fees it is a clear reject.

## 11. Acceptance criteria (from the issue) — how this design meets each

- Claude-readable strategy-discovery plan exists → this note (§2 funnel, §4–6).
- Fast screening command emits a structured artifact → `discover.py` + §7.
- Output distinguishes `screened_out` / `needs_more_data` /
  `promote_to_full_validation` → §6 classifier.
- `--window-from/--window-to` is a real data constraint with tests → §5.2 + §9
  `test_discovery_window.py` (Nautilus-path window documented as PR2).
- No full-validation result marketed as `validated` unless it passes the gate →
  discovery is non-canonical by construction (§6, §7 note); gate unchanged.
- PR description carries the safety boundary → §12, restated at PR time.
- PR includes a recommended next strategy family (or why all were rejected) →
  produced from the first real discovery run; expected, per the honest prior, to be
  "most families screen out on fees", with the least-bad family named.

## 12. Safety boundary

Research only. Public trade-tick data, isolated venv, read-only catalog access. No
live trading, no Demo `confirm=true`, no broker/order/watch/order-intent mutation,
no scheduler/Prefect/launchd, no prod DB/env/secrets, no runtime parameter
application, no `/invest` surfacing. The discovery harness imports nothing from
`app/` and nothing from the Nautilus engine. Artifacts contain no secrets,
balances, positions, or account identifiers.
