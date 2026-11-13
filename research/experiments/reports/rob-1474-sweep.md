# ROB-1474 multi-hypothesis sweep: exploration result, no validation look

Experiment-Manifest-Sha256: a48d80bcd2b6a7eb283e063454c83f6f216302568d836ae817328074e3880750
Experiment-Test-Look: no

Hypothesis `ROB-1474-sweep`; results ran under manifest version 2 (hash above), now superseded by version 5 (see below); registry `k` before = 0, after = 0.
Research only: public data, no database, broker, credential, scheduler or `app/` change.
Ratios and counts only. This report makes **no promotion or deployment claim**.

## Result in one paragraph

Seven simple rankers x five horizons x long-only/long-short x two markets (US, crypto) = **140 cells**
were run once on the exploration window (decisions 2021-01-04..2023-12-31, exits inside it).
**No cell met the pre-registered carry-forward rule** (largest Newey-West t = 1.06; the rule needs 2.0), so the
validation window (2024-02-01..2024-12-31) was **never read** and the one test look was **not spent**.
Per-cell results below are exploration diagnostics, not evidence of an edge.

## Order of commits (acceptance 1)

The registry entry and manifest are committed before any result exists, and the results commit
precedes any validation read (there was none):

```
619c02caf hk-3645: ROB-1474-sweep exploration results (manifest v2): no cell carried forward
9d08d8e1d hk-3645: manifest v2 - outcomes need every session between entry and exit
a9172ad6f hk-3645: register ROB-1474-sweep (manifest v1, k=0) with the sweep harness
```

Manifest v1 (`dc2512f8...`) was replaced by v2 (above hash) **before the rerun and before any look**. The v1
exploration run checked only the entry and exit opens of a hold; a crypto token swap with three missing sessions
in between (price x1000) produced outcomes above +100,000%. v2 requires every session of the hold to exist.
The 140 v1 rows stay in the ledger flagged `superseded_exploration`; no v1 number is used below.

Manifests v3, v4 and v5 (registered after review of this PR, before any look, `k` still 0) change only run hygiene: the ledger and
carry files are write-once, and a US cache fetched for an earlier period, or without a recorded request window, is refused, and the version stamped on new ledger rows is read from the registered manifest. The v2 code
(`exploration_run_code_sha256` in the v3, v4 and v5 manifests) produced the committed results; a rerun with the v5 code into a fresh
directory reproduces all 140 exploration cell metrics exactly and the same empty carry list.

## Design (fixed in the manifest)

- Decision at the close of session i; entry at the open of i+1, exit at the open of i+1+h (an h-session hold),
  h in 1, 3, 5, 10, 20. A series that ended early exits at its last close (terminal convention, not a payoff claim).
  A hole in any session of the hold leaves the outcome unknown; unknown outcomes are dropped after selection.
- Rankers: momentum 20, momentum 60, reversal 5, reversal 1, low volatility 20, distance to 52-week high
  (252 US sessions / 364 crypto days), volume shock 20. Eligible = all features known, positive volume,
  top 100 (US) / 60 (crypto) by 20-session mean traded value. Decile size = ceil(10% of N) (10 US, 6 crypto on average).
- Sides: long-only top decile; long-short top minus bottom decile. Cost 10 bps (US) / 15 bps (crypto) per side;
  2 sides long-only, 4 sides long-short, identical for every control.
- Controls on the same dates, names, horizon and cost: 200 seeded random selections of the same size
  (long-short draws disjoint sets), the equal-weight eligible universe, and buy-and-hold of SPY (US) / BTCUSDT (crypto).
- **Primary series**: long-only = top decile minus equal-weight universe; long-short = top minus bottom minus 4 sides of cost.
  The long-only excess over the benchmark is reported separately. For long-short, the equal-weight and
  benchmark controls are market-neutral (zero gross), so the long-short net spread is its excess over them; the legs
  are also split into excess over equal-weight and benchmark in the ledger.
- **Random rank p** = (1 + number of the 200 seeds whose mean is at least the cell's mean) / 201. Random mean and its
  [5th, 95th] percentile over seeds are listed with every result line (for long-short they include the cost drag).
- Newey-West t of the primary series, lags = horizon. Year means are for 2021, 2022, 2023.

## Trials ledger (acceptance 3)

`research/selection_sweep/results/trials_ledger.jsonl` has **282 rows** = grid size **140** (manifest v2, phase `exploration`)
+ **142 extra** rows: 140 superseded v1 cell rows, 1 v2 input-coverage row, 1 superseded v1 input-coverage row.
`carry_forward.json` records the empty carry list and the code hash. Per-date arrays (hash
`e8f9339b76773c75806235bc75ffb994d9d82675383ceb83379020423f88d5e3`, file kept outside the repository) hold the daily strategy and
control series.

## Findings

- 33 of 140 cells have a positive primary mean (26 of 70 long-only, 7 of 70 long-short). 14 also have random rank p <= 0.05
  (the table below). With 140 correlated cells, roughly 7 would pass p <= 0.05 by chance.
- **Largest Newey-West t is 1.06; none reaches 2.0.** Random rank p only compares a ranker with random picking on the same
  dates. It does not carry the dispersion of the date process, so p = 0.005 can coexist with t near 1
  (US low volatility, long-only, h = 5/10/20: +14/+29/+48 bps per vintage, positive in 2 of 3 years, t = 0.98/1.06/0.89).
  That pattern is consistent with a defensive tilt (low beta) rather than selection skill; beta was not controlled.
- Most long-short cells are negative net of four sides of cost. Positive long-short cells are at h = 10/20,
  where cost is a smaller share, and their long legs do not beat the benchmark in every case.
- Crypto momentum 60 and the reversal rankers rank at or near the bottom of the random distribution in most cells
  (random rank p >= 0.93 in the long-only grid); no inference beyond this window.
- At most 0.07% of eligible name-vintages have an unknown outcome (crypto), 0.00% in the US sample.

## Survivor and point-in-time limits (read before using any number)

- **US**: not a full market. A seeded sample of 900 common stocks (seed 20261109) listed at 2019-12-31 from the
  point-in-time listing table; **550 had prices on the public source, 350 did not** (290 HTTP 404, 59 HTTP 400, 1 parse error).
  Names delisted before or during the window are mostly among the missing, so the US panel is **survivor-biased**
  (zero terminated series). Survivorship usually inflates long-side returns and hides negative tails; its effect on
  spreads and on the sign of any ranker is not guaranteed. Prices are the provider's current adjusted vintage; traded value is close x volume.
- **Crypto**: Binance USDT spot, point-in-time and delisted-inclusive (408 pairs after removing 50 leveraged tokens and 19
  stable-base pairs; 49 series end before the window end). Terminal exits at the last close are optimistic compared with real wind-downs.
  BTCUSDT is both a universe member and the benchmark. Holdout candle files after 2024-12-31 are never opened;
  the loader truncates all inputs at 2023-12-31 in this run (the exploration files on disk also hold 2024 rows, which are read from disk and discarded).
- Open-to-open fills with a flat per-side cost; no slippage, capacity or borrow model; shorts are assumed free to borrow.
  This is not an executable NAV and not a market-wide readiness claim.
- KR is out of scope (KR boosting is STOP). No TFT.

## What was not done

- **No validation look.** The carry-forward rule found no cell. Nothing from 2024 was fetched for the US or loaded for crypto.
- No re-parameterisation, new ranker or new threshold after seeing results; the rule is not relaxed. If someone wants to test
  the US low-volatility long-only cells, that needs a **new pre-registered manifest and a new, untouched test window**; they are
  at most candidates for such a separate pre-registered test, with the beta caveat above. Paper results must not select or tune them.

## Reproduce

```bash
uv run pytest tests/research/selection_sweep -q
uv run python scripts/ci/experiment_registry_lint.py
# explore refuses to overwrite the committed ledger; reproduce into a fresh results directory
uv run python -m research.selection_sweep.cli explore --us-cache <dir> --crypto-root <dir> --crypto-pit <csv> --out-dir <dir> --results-dir <fresh dir>
uv run python -m research.selection_sweep.reporting.tables --results-dir <fresh dir> --check-against-committed
```

The last command exits 1 unless every exploration cell metric in the fresh ledger equals the committed one.

Tables: each cell is `primary bps / random rank p`; `*` would mark a carried cell (none). Primary bps = mean net excess per vintage.

## Results (exploration window)


#### us long_only

| ranker | h=1 | h=3 | h=5 | h=10 | h=20 |
|---|---|---|---|---|---|
| momentum_20 | +2 / p0.23 | +4 / p0.17 | -0 / p0.49 | -10 / p0.89 | -75 / p1.00 |
| momentum_60 | -4 / p0.96 | -13 / p1.00 | -16 / p1.00 | -37 / p1.00 | -83 / p1.00 |
| reversal_5 | +4 / p0.10 | +9 / p0.02 | +2 / p0.35 | +6 / p0.22 | -20 / p0.96 |
| reversal_1 | -1 / p0.67 | +2 / p0.34 | +10 / p0.03 | +3 / p0.35 | -25 / p1.00 |
| low_volatility_20 | +2 / p0.23 | +7 / p0.04 | +14 / p0.01 | +29 / p0.00 | +48 / p0.00 |
| high_distance_52w | -1 / p0.70 | +0 / p0.45 | -5 / p0.78 | -5 / p0.74 | -8 / p0.79 |
| volume_shock_20 | +0 / p0.43 | -3 / p0.70 | -1 / p0.54 | -12 / p0.91 | -4 / p0.64 |

#### us long_short

| ranker | h=1 | h=3 | h=5 | h=10 | h=20 |
|---|---|---|---|---|---|
| momentum_20 | -31 / p0.01 | -21 / p0.01 | -21 / p0.01 | -35 / p0.28 | -92 / p1.00 |
| momentum_60 | -42 / p0.71 | -48 / p0.87 | -52 / p0.91 | -80 / p1.00 | -137 / p1.00 |
| reversal_5 | -34 / p0.07 | -29 / p0.04 | -30 / p0.10 | -20 / p0.04 | -43 / p0.56 |
| reversal_1 | -38 / p0.35 | -31 / p0.07 | -15 / p0.00 | -24 / p0.07 | -53 / p0.79 |
| low_volatility_20 | -33 / p0.04 | -19 / p0.00 | -6 / p0.00 | +37 / p0.00 | +94 / p0.00 |
| high_distance_52w | -33 / p0.04 | -17 / p0.00 | -9 / p0.00 | +18 / p0.00 | +83 / p0.00 |
| volume_shock_20 | -37 / p0.24 | -32 / p0.10 | -26 / p0.05 | -15 / p0.02 | +21 / p0.00 |

#### crypto long_only

| ranker | h=1 | h=3 | h=5 | h=10 | h=20 |
|---|---|---|---|---|---|
| momentum_20 | +11 / p0.06 | +1 / p0.48 | +2 / p0.46 | -34 / p0.90 | -98 / p1.00 |
| momentum_60 | -36 / p1.00 | -97 / p1.00 | -150 / p1.00 | -309 / p1.00 | -492 / p1.00 |
| reversal_5 | -16 / p1.00 | -64 / p1.00 | -95 / p1.00 | -101 / p1.00 | -149 / p1.00 |
| reversal_1 | -13 / p0.98 | -16 / p0.93 | -47 / p1.00 | -72 / p1.00 | -163 / p1.00 |
| low_volatility_20 | +6 / p0.21 | +11 / p0.18 | +9 / p0.29 | -15 / p0.72 | -72 / p0.99 |
| high_distance_52w | +9 / p0.08 | +20 / p0.03 | +6 / p0.35 | -25 / p0.84 | -89 / p1.00 |
| volume_shock_20 | -29 / p1.00 | -12 / p0.86 | +1 / p0.46 | -43 / p0.96 | -3 / p0.51 |

#### crypto long_short

| ranker | h=1 | h=3 | h=5 | h=10 | h=20 |
|---|---|---|---|---|---|
| momentum_20 | -44 / p0.05 | -41 / p0.14 | -24 / p0.08 | +14 / p0.03 | +107 / p0.00 |
| momentum_60 | -92 / p1.00 | -145 / p1.00 | -200 / p1.00 | -326 / p1.00 | -360 / p1.00 |
| reversal_5 | -88 / p1.00 | -173 / p1.00 | -193 / p1.00 | -121 / p0.95 | -162 / p0.97 |
| reversal_1 | -42 / p0.05 | -55 / p0.36 | -101 / p0.96 | -90 / p0.79 | -185 / p0.99 |
| low_volatility_20 | -59 / p0.44 | -46 / p0.20 | -43 / p0.26 | -55 / p0.48 | -278 / p1.00 |
| high_distance_52w | -55 / p0.31 | -38 / p0.10 | -40 / p0.19 | -55 / p0.48 | -129 / p0.92 |
| volume_shock_20 | -95 / p1.00 | -69 / p0.69 | -45 / p0.26 | -134 / p0.97 | -138 / p0.92 |

#### random p <= 0.05 and primary > 0: 14 cells

| cell | primary bps | long leg vs benchmark bps | random mean [p05, p95] bps | random p | NW t | years + | dates |
|---|---|---|---|---|---|---|---|
| us / reversal_5 / h3 / long_only | +9 | +6 | -0 [-8, +7] | 0.025 | 0.73 | 1/3 | 749 |
| us / reversal_1 / h5 / long_only | +10 | +5 | -0 [-9, +9] | 0.035 | 0.73 | 1/3 | 747 |
| us / low_volatility_20 / h3 / long_only | +7 | +4 | -0 [-8, +7] | 0.040 | 0.78 | 2/3 | 749 |
| us / low_volatility_20 / h5 / long_only | +14 | +9 | -0 [-9, +9] | 0.010 | 0.98 | 2/3 | 747 |
| us / low_volatility_20 / h10 / long_only | +29 | +15 | -0 [-14, +13] | 0.005 | 1.06 | 2/3 | 742 |
| us / low_volatility_20 / h10 / long_short | +37 | +15 | -41 [-59, -24] | 0.005 | 0.39 | 2/3 | 742 |
| us / low_volatility_20 / h20 / long_only | +48 | +18 | +1 [-18, +19] | 0.005 | 0.89 | 2/3 | 732 |
| us / low_volatility_20 / h20 / long_short | +94 | +18 | -40 [-67, -13] | 0.005 | 0.51 | 2/3 | 732 |
| us / high_distance_52w / h10 / long_short | +18 | -19 | -41 [-59, -24] | 0.005 | 0.20 | 2/3 | 742 |
| us / high_distance_52w / h20 / long_short | +83 | -38 | -40 [-67, -13] | 0.005 | 0.46 | 2/3 | 732 |
| us / volume_shock_20 / h20 / long_short | +21 | -34 | -40 [-67, -13] | 0.005 | 0.56 | 2/3 | 732 |
| crypto / momentum_20 / h10 / long_short | +14 | +105 | -58 [-122, +6] | 0.035 | 0.10 | 2/3 | 1081 |
| crypto / momentum_20 / h20 / long_short | +107 | +205 | -55 [-152, +57] | 0.005 | 0.47 | 2/3 | 1071 |
| crypto / high_distance_52w / h3 / long_only | +20 | +54 | +0 [-19, +19] | 0.035 | 0.95 | 1/3 | 1088 |

#### every cell

| cell | primary bps | long leg vs benchmark bps | random mean [p05, p95] bps | random p | NW t | years + | dates |
|---|---|---|---|---|---|---|---|
| us / momentum_20 / h1 / long_only | +2 | +1 | +0 [-4, +5] | 0.234 | 0.36 | 2/3 | 751 |
| us / momentum_20 / h1 / long_short | -31 | +1 | -40 [-46, -33] | 0.015 | -3.35 | 0/3 | 751 |
| us / momentum_20 / h3 / long_only | +4 | +2 | -0 [-8, +7] | 0.174 | 0.31 | 2/3 | 749 |
| us / momentum_20 / h3 / long_short | -21 | +2 | -40 [-51, -30] | 0.010 | -0.97 | 0/3 | 749 |
| us / momentum_20 / h5 / long_only | -0 | -5 | -0 [-9, +9] | 0.488 | -0.00 | 1/3 | 747 |
| us / momentum_20 / h5 / long_short | -21 | -5 | -40 [-55, -27] | 0.015 | -0.67 | 1/3 | 747 |
| us / momentum_20 / h10 / long_only | -10 | -24 | -0 [-14, +13] | 0.891 | -0.25 | 1/3 | 742 |
| us / momentum_20 / h10 / long_short | -35 | -24 | -41 [-59, -24] | 0.284 | -0.63 | 1/3 | 742 |
| us / momentum_20 / h20 / long_only | -75 | -104 | +1 [-18, +19] | 1.000 | -1.04 | 1/3 | 732 |
| us / momentum_20 / h20 / long_short | -92 | -104 | -40 [-67, -13] | 1.000 | -0.88 | 1/3 | 732 |
| us / momentum_60 / h1 / long_only | -4 | -5 | +0 [-4, +5] | 0.955 | -0.76 | 0/3 | 751 |
| us / momentum_60 / h1 / long_short | -42 | -5 | -40 [-46, -33] | 0.711 | -4.21 | 0/3 | 751 |
| us / momentum_60 / h3 / long_only | -13 | -16 | -0 [-8, +7] | 1.000 | -0.87 | 0/3 | 749 |
| us / momentum_60 / h3 / long_short | -48 | -16 | -40 [-51, -30] | 0.871 | -1.97 | 0/3 | 749 |
| us / momentum_60 / h5 / long_only | -16 | -21 | -0 [-9, +9] | 1.000 | -0.67 | 1/3 | 747 |
| us / momentum_60 / h5 / long_short | -52 | -21 | -40 [-55, -27] | 0.905 | -1.39 | 0/3 | 747 |
| us / momentum_60 / h10 / long_only | -37 | -50 | -0 [-14, +13] | 1.000 | -0.82 | 1/3 | 742 |
| us / momentum_60 / h10 / long_short | -80 | -50 | -41 [-59, -24] | 1.000 | -1.22 | 0/3 | 742 |
| us / momentum_60 / h20 / long_only | -83 | -113 | +1 [-18, +19] | 1.000 | -1.12 | 1/3 | 732 |
| us / momentum_60 / h20 / long_short | -137 | -113 | -40 [-67, -13] | 1.000 | -1.10 | 0/3 | 732 |
| us / reversal_5 / h1 / long_only | +4 | +3 | +0 [-4, +5] | 0.104 | 0.66 | 2/3 | 751 |
| us / reversal_5 / h1 / long_short | -34 | +3 | -40 [-46, -33] | 0.070 | -3.74 | 0/3 | 751 |
| us / reversal_5 / h3 / long_only | +9 | +6 | -0 [-8, +7] | 0.025 | 0.73 | 1/3 | 749 |
| us / reversal_5 / h3 / long_short | -29 | +6 | -40 [-51, -30] | 0.040 | -1.65 | 0/3 | 749 |
| us / reversal_5 / h5 / long_only | +2 | -3 | -0 [-9, +9] | 0.348 | 0.14 | 1/3 | 747 |
| us / reversal_5 / h5 / long_short | -30 | -3 | -40 [-55, -27] | 0.104 | -1.21 | 0/3 | 747 |
| us / reversal_5 / h10 / long_only | +6 | -7 | -0 [-14, +13] | 0.224 | 0.18 | 2/3 | 742 |
| us / reversal_5 / h10 / long_short | -20 | -7 | -41 [-59, -24] | 0.040 | -0.48 | 1/3 | 742 |
| us / reversal_5 / h20 / long_only | -20 | -50 | +1 [-18, +19] | 0.960 | -0.30 | 1/3 | 732 |
| us / reversal_5 / h20 / long_short | -43 | -50 | -40 [-67, -13] | 0.562 | -0.66 | 1/3 | 732 |
| us / reversal_1 / h1 / long_only | -1 | -2 | +0 [-4, +5] | 0.667 | -0.17 | 1/3 | 751 |
| us / reversal_1 / h1 / long_short | -38 | -2 | -40 [-46, -33] | 0.348 | -4.35 | 0/3 | 751 |
| us / reversal_1 / h3 / long_only | +2 | -1 | -0 [-8, +7] | 0.343 | 0.15 | 1/3 | 749 |
| us / reversal_1 / h3 / long_short | -31 | -1 | -40 [-51, -30] | 0.075 | -2.04 | 0/3 | 749 |
| us / reversal_1 / h5 / long_only | +10 | +5 | -0 [-9, +9] | 0.035 | 0.73 | 1/3 | 747 |
| us / reversal_1 / h5 / long_short | -15 | +5 | -40 [-55, -27] | 0.005 | -0.86 | 0/3 | 747 |
| us / reversal_1 / h10 / long_only | +3 | -11 | -0 [-14, +13] | 0.353 | 0.12 | 1/3 | 742 |
| us / reversal_1 / h10 / long_short | -24 | -11 | -41 [-59, -24] | 0.075 | -1.07 | 0/3 | 742 |
| us / reversal_1 / h20 / long_only | -25 | -55 | +1 [-18, +19] | 0.995 | -0.54 | 1/3 | 732 |
| us / reversal_1 / h20 / long_short | -53 | -55 | -40 [-67, -13] | 0.791 | -1.76 | 0/3 | 732 |
| us / low_volatility_20 / h1 / long_only | +2 | +1 | +0 [-4, +5] | 0.229 | 0.61 | 2/3 | 751 |
| us / low_volatility_20 / h1 / long_short | -33 | +1 | -40 [-46, -33] | 0.045 | -2.69 | 0/3 | 751 |
| us / low_volatility_20 / h3 / long_only | +7 | +4 | -0 [-8, +7] | 0.040 | 0.78 | 2/3 | 749 |
| us / low_volatility_20 / h3 / long_short | -19 | +4 | -40 [-51, -30] | 0.005 | -0.63 | 1/3 | 749 |
| us / low_volatility_20 / h5 / long_only | +14 | +9 | -0 [-9, +9] | 0.010 | 0.98 | 2/3 | 747 |
| us / low_volatility_20 / h5 / long_short | -6 | +9 | -40 [-55, -27] | 0.005 | -0.12 | 2/3 | 747 |
| us / low_volatility_20 / h10 / long_only | +29 | +15 | -0 [-14, +13] | 0.005 | 1.06 | 2/3 | 742 |
| us / low_volatility_20 / h10 / long_short | +37 | +15 | -41 [-59, -24] | 0.005 | 0.39 | 2/3 | 742 |
| us / low_volatility_20 / h20 / long_only | +48 | +18 | +1 [-18, +19] | 0.005 | 0.89 | 2/3 | 732 |
| us / low_volatility_20 / h20 / long_short | +94 | +18 | -40 [-67, -13] | 0.005 | 0.51 | 2/3 | 732 |
| us / high_distance_52w / h1 / long_only | -1 | -2 | +0 [-4, +5] | 0.697 | -0.33 | 2/3 | 751 |
| us / high_distance_52w / h1 / long_short | -33 | -2 | -40 [-46, -33] | 0.045 | -2.82 | 0/3 | 751 |
| us / high_distance_52w / h3 / long_only | +0 | -3 | -0 [-8, +7] | 0.453 | 0.04 | 2/3 | 749 |
| us / high_distance_52w / h3 / long_short | -17 | -3 | -40 [-51, -30] | 0.005 | -0.58 | 2/3 | 749 |
| us / high_distance_52w / h5 / long_only | -5 | -10 | -0 [-9, +9] | 0.776 | -0.36 | 2/3 | 747 |
| us / high_distance_52w / h5 / long_short | -9 | -10 | -40 [-55, -27] | 0.005 | -0.20 | 2/3 | 747 |
| us / high_distance_52w / h10 / long_only | -5 | -19 | -0 [-14, +13] | 0.741 | -0.19 | 2/3 | 742 |
| us / high_distance_52w / h10 / long_short | +18 | -19 | -41 [-59, -24] | 0.005 | 0.20 | 2/3 | 742 |
| us / high_distance_52w / h20 / long_only | -8 | -38 | +1 [-18, +19] | 0.791 | -0.15 | 2/3 | 732 |
| us / high_distance_52w / h20 / long_short | +83 | -38 | -40 [-67, -13] | 0.005 | 0.46 | 2/3 | 732 |
| us / volume_shock_20 / h1 / long_only | +0 | -1 | +0 [-4, +5] | 0.433 | 0.07 | 1/3 | 751 |
| us / volume_shock_20 / h1 / long_short | -37 | -1 | -40 [-46, -33] | 0.239 | -6.79 | 0/3 | 751 |
| us / volume_shock_20 / h3 / long_only | -3 | -5 | -0 [-8, +7] | 0.701 | -0.34 | 1/3 | 749 |
| us / volume_shock_20 / h3 / long_short | -32 | -5 | -40 [-51, -30] | 0.104 | -2.83 | 0/3 | 749 |
| us / volume_shock_20 / h5 / long_only | -1 | -6 | -0 [-9, +9] | 0.537 | -0.08 | 1/3 | 747 |
| us / volume_shock_20 / h5 / long_short | -26 | -6 | -40 [-55, -27] | 0.050 | -1.50 | 1/3 | 747 |
| us / volume_shock_20 / h10 / long_only | -12 | -25 | -0 [-14, +13] | 0.905 | -0.77 | 1/3 | 742 |
| us / volume_shock_20 / h10 / long_short | -15 | -25 | -41 [-59, -24] | 0.020 | -0.48 | 1/3 | 742 |
| us / volume_shock_20 / h20 / long_only | -4 | -34 | +1 [-18, +19] | 0.637 | -0.18 | 2/3 | 732 |
| us / volume_shock_20 / h20 / long_short | +21 | -34 | -40 [-67, -13] | 0.005 | 0.56 | 2/3 | 732 |
| crypto / momentum_20 / h1 / long_only | +11 | +21 | -0 [-12, +11] | 0.060 | 0.65 | 2/3 | 1090 |
| crypto / momentum_20 / h1 / long_short | -44 | +21 | -61 [-79, -44] | 0.055 | -2.12 | 0/3 | 1090 |
| crypto / momentum_20 / h3 / long_only | +1 | +35 | +0 [-19, +19] | 0.483 | 0.04 | 2/3 | 1088 |
| crypto / momentum_20 / h3 / long_short | -41 | +35 | -61 [-89, -31] | 0.144 | -0.90 | 2/3 | 1088 |
| crypto / momentum_20 / h5 / long_only | +2 | +64 | -0 [-25, +25] | 0.458 | 0.03 | 2/3 | 1086 |
| crypto / momentum_20 / h5 / long_short | -24 | +64 | -61 [-100, -19] | 0.085 | -0.34 | 2/3 | 1086 |
| crypto / momentum_20 / h10 / long_only | -34 | +105 | -0 [-40, +43] | 0.900 | -0.32 | 2/3 | 1081 |
| crypto / momentum_20 / h10 / long_short | +14 | +105 | -58 [-122, +6] | 0.035 | 0.10 | 2/3 | 1081 |
| crypto / momentum_20 / h20 / long_only | -98 | +205 | +1 [-58, +75] | 1.000 | -0.61 | 2/3 | 1071 |
| crypto / momentum_20 / h20 / long_short | +107 | +205 | -55 [-152, +57] | 0.005 | 0.47 | 2/3 | 1071 |
| crypto / momentum_60 / h1 / long_only | -36 | -25 | -0 [-12, +11] | 1.000 | -3.06 | 0/3 | 1090 |
| crypto / momentum_60 / h1 / long_short | -92 | -25 | -61 [-79, -44] | 1.000 | -5.89 | 0/3 | 1090 |
| crypto / momentum_60 / h3 / long_only | -97 | -64 | +0 [-19, +19] | 1.000 | -3.34 | 0/3 | 1088 |
| crypto / momentum_60 / h3 / long_short | -145 | -64 | -61 [-89, -31] | 1.000 | -3.79 | 0/3 | 1088 |
| crypto / momentum_60 / h5 / long_only | -150 | -87 | -0 [-25, +25] | 1.000 | -3.29 | 0/3 | 1086 |
| crypto / momentum_60 / h5 / long_short | -200 | -87 | -61 [-100, -19] | 1.000 | -3.25 | 0/3 | 1086 |
| crypto / momentum_60 / h10 / long_only | -309 | -170 | -0 [-40, +43] | 1.000 | -3.92 | 0/3 | 1081 |
| crypto / momentum_60 / h10 / long_short | -326 | -170 | -58 [-122, +6] | 1.000 | -2.80 | 0/3 | 1081 |
| crypto / momentum_60 / h20 / long_only | -492 | -189 | +1 [-58, +75] | 1.000 | -3.71 | 0/3 | 1071 |
| crypto / momentum_60 / h20 / long_short | -360 | -189 | -55 [-152, +57] | 1.000 | -1.58 | 0/3 | 1071 |
| crypto / reversal_5 / h1 / long_only | -16 | -5 | -0 [-12, +11] | 0.995 | -1.80 | 0/3 | 1090 |
| crypto / reversal_5 / h1 / long_short | -88 | -5 | -61 [-79, -44] | 1.000 | -4.22 | 0/3 | 1090 |
| crypto / reversal_5 / h3 / long_only | -64 | -31 | +0 [-19, +19] | 1.000 | -3.25 | 0/3 | 1088 |
| crypto / reversal_5 / h3 / long_short | -173 | -31 | -61 [-89, -31] | 1.000 | -4.00 | 0/3 | 1088 |
| crypto / reversal_5 / h5 / long_only | -95 | -32 | -0 [-25, +25] | 1.000 | -3.37 | 0/3 | 1086 |
| crypto / reversal_5 / h5 / long_short | -193 | -32 | -61 [-100, -19] | 1.000 | -3.16 | 0/3 | 1086 |
| crypto / reversal_5 / h10 / long_only | -101 | +38 | -0 [-40, +43] | 1.000 | -2.17 | 0/3 | 1081 |
| crypto / reversal_5 / h10 / long_short | -121 | +38 | -58 [-122, +6] | 0.945 | -1.14 | 2/3 | 1081 |
| crypto / reversal_5 / h20 / long_only | -149 | +154 | +1 [-58, +75] | 1.000 | -2.16 | 0/3 | 1071 |
| crypto / reversal_5 / h20 / long_short | -162 | +154 | -55 [-152, +57] | 0.965 | -1.08 | 1/3 | 1071 |
| crypto / reversal_1 / h1 / long_only | -13 | -2 | -0 [-12, +11] | 0.980 | -1.27 | 0/3 | 1090 |
| crypto / reversal_1 / h1 / long_short | -42 | -2 | -61 [-79, -44] | 0.050 | -2.22 | 0/3 | 1090 |
| crypto / reversal_1 / h3 / long_only | -16 | +17 | +0 [-19, +19] | 0.930 | -0.82 | 1/3 | 1088 |
| crypto / reversal_1 / h3 / long_short | -55 | +17 | -61 [-89, -31] | 0.358 | -1.73 | 1/3 | 1088 |
| crypto / reversal_1 / h5 / long_only | -47 | +15 | -0 [-25, +25] | 1.000 | -1.80 | 0/3 | 1086 |
| crypto / reversal_1 / h5 / long_short | -101 | +15 | -61 [-100, -19] | 0.955 | -2.39 | 0/3 | 1086 |
| crypto / reversal_1 / h10 / long_only | -72 | +67 | -0 [-40, +43] | 0.995 | -1.84 | 0/3 | 1081 |
| crypto / reversal_1 / h10 / long_short | -90 | +67 | -58 [-122, +6] | 0.786 | -1.31 | 2/3 | 1081 |
| crypto / reversal_1 / h20 / long_only | -163 | +140 | +1 [-58, +75] | 1.000 | -2.34 | 0/3 | 1071 |
| crypto / reversal_1 / h20 / long_short | -185 | +140 | -55 [-152, +57] | 0.985 | -1.69 | 2/3 | 1071 |
| crypto / low_volatility_20 / h1 / long_only | +6 | +17 | -0 [-12, +11] | 0.214 | 0.70 | 2/3 | 1090 |
| crypto / low_volatility_20 / h1 / long_short | -59 | +17 | -61 [-79, -44] | 0.443 | -3.04 | 0/3 | 1090 |
| crypto / low_volatility_20 / h3 / long_only | +11 | +44 | +0 [-19, +19] | 0.179 | 0.50 | 1/3 | 1088 |
| crypto / low_volatility_20 / h3 / long_short | -46 | +44 | -61 [-89, -31] | 0.199 | -1.04 | 1/3 | 1088 |
| crypto / low_volatility_20 / h5 / long_only | +9 | +71 | -0 [-25, +25] | 0.289 | 0.27 | 1/3 | 1086 |
| crypto / low_volatility_20 / h5 / long_short | -43 | +71 | -61 [-100, -19] | 0.259 | -0.63 | 1/3 | 1086 |
| crypto / low_volatility_20 / h10 / long_only | -15 | +124 | -0 [-40, +43] | 0.716 | -0.25 | 1/3 | 1081 |
| crypto / low_volatility_20 / h10 / long_short | -55 | +124 | -58 [-122, +6] | 0.478 | -0.44 | 1/3 | 1081 |
| crypto / low_volatility_20 / h20 / long_only | -72 | +231 | +1 [-58, +75] | 0.985 | -0.56 | 1/3 | 1071 |
| crypto / low_volatility_20 / h20 / long_short | -278 | +231 | -55 [-152, +57] | 1.000 | -1.05 | 1/3 | 1071 |
| crypto / high_distance_52w / h1 / long_only | +9 | +20 | -0 [-12, +11] | 0.085 | 1.03 | 1/3 | 1090 |
| crypto / high_distance_52w / h1 / long_short | -55 | +20 | -61 [-79, -44] | 0.313 | -4.10 | 0/3 | 1090 |
| crypto / high_distance_52w / h3 / long_only | +20 | +54 | +0 [-19, +19] | 0.035 | 0.95 | 1/3 | 1088 |
| crypto / high_distance_52w / h3 / long_short | -38 | +54 | -61 [-89, -31] | 0.104 | -1.13 | 1/3 | 1088 |
| crypto / high_distance_52w / h5 / long_only | +6 | +69 | -0 [-25, +25] | 0.353 | 0.18 | 1/3 | 1086 |
| crypto / high_distance_52w / h5 / long_short | -40 | +69 | -61 [-100, -19] | 0.189 | -0.75 | 1/3 | 1086 |
| crypto / high_distance_52w / h10 / long_only | -25 | +114 | -0 [-40, +43] | 0.836 | -0.36 | 1/3 | 1081 |
| crypto / high_distance_52w / h10 / long_short | -55 | +114 | -58 [-122, +6] | 0.478 | -0.50 | 1/3 | 1081 |
| crypto / high_distance_52w / h20 / long_only | -89 | +214 | +1 [-58, +75] | 1.000 | -0.85 | 1/3 | 1071 |
| crypto / high_distance_52w / h20 / long_short | -129 | +214 | -55 [-152, +57] | 0.915 | -0.65 | 1/3 | 1071 |
| crypto / volume_shock_20 / h1 / long_only | -29 | -18 | -0 [-12, +11] | 1.000 | -2.10 | 1/3 | 1090 |
| crypto / volume_shock_20 / h1 / long_short | -95 | -18 | -61 [-79, -44] | 1.000 | -5.56 | 0/3 | 1090 |
| crypto / volume_shock_20 / h3 / long_only | -12 | +21 | +0 [-19, +19] | 0.861 | -0.46 | 1/3 | 1088 |
| crypto / volume_shock_20 / h3 / long_short | -69 | +21 | -61 [-89, -31] | 0.692 | -2.01 | 1/3 | 1088 |
| crypto / volume_shock_20 / h5 / long_only | +1 | +64 | -0 [-25, +25] | 0.463 | 0.02 | 1/3 | 1086 |
| crypto / volume_shock_20 / h5 / long_short | -45 | +64 | -61 [-100, -19] | 0.264 | -0.87 | 1/3 | 1086 |
| crypto / volume_shock_20 / h10 / long_only | -43 | +95 | -0 [-40, +43] | 0.960 | -0.59 | 1/3 | 1081 |
| crypto / volume_shock_20 / h10 / long_short | -134 | +95 | -58 [-122, +6] | 0.965 | -1.23 | 1/3 | 1081 |
| crypto / volume_shock_20 / h20 / long_only | -3 | +300 | +1 [-58, +75] | 0.507 | -0.03 | 1/3 | 1071 |
| crypto / volume_shock_20 / h20 / long_short | -138 | +300 | -55 [-152, +57] | 0.920 | -0.77 | 1/3 | 1071 |
