# ROB-382 — strat.ninja external-strategy falsification spike

**Status:** complete. **Verdict: `no_decisive_survivor` → close the line, open NO backtest issue.**

A bounded, read-only falsification spike. We took the top strat.ninja freqtrade strategies,
ported ONLY their signal logic into our own `research/nautilus_scalping` harness, and
re-validated each on Binance USDⓈ-M data under our frozen cost model + OOS gate. This is
**falsification of the leaderboard under our standard, not adoption of it.** strat.ninja's
spot backtest numbers are used ONLY for the contrast column — never as evidence.

## TL;DR

- **4 candidates** ported, all non-ICT (ICT excluded — already covered by `ict_signal.py`),
  each at its **native timeframe** (timeframe-faithful, not coerced to short-horizon).
- **New information vs our own families:** unlike our self-generated short-horizon families
  (ROB-353: all gross-NEGATIVE at −70.99 / −27.53 / −39.38 bps), these external dip-buy
  strategies clear the **gross** screen (positive gross at native timeframe) and **beat the
  micro-breakout + random-entry baselines**.
- **But NONE clears the decisive bar** (`gross + t>2 OOS + beats-baselines + net-positive at
  our taker fee`):
  - `ichi` (ichiV1) is the only one with enough trades to pass the full walk-forward gate
    (`validated`), but its **OOS t-stat is 1.19 (< 2.0)** and it is **net-NEGATIVE at the
    retail taker fee** (−6.27 bps OOS; only +5.73 bps at the 4 bps/leg VIP/demo schedule).
  - `elliot` / `vwap` / `cluc` show large gross but are **under-powered** (18 / 43 / 198 trades
    on a 4-symbol universe → the walk-forward gate returns `insufficient_data`); OOS t ≤ 1.68.
- **Decision:** external leaderboards do not survive our OOS USDⓈ-M robustness gate. Close the
  line. Open **no** backtest issue. (Re-confirms the "but what about proven strategies?"
  question, now including the cleaner, lookahead-filtered strat.ninja pool.)

## Method

`research/nautilus_scalping/run_rob382_falsification.py` (driver) + `rob382_runner.py`:

1. Port each strategy's entry/exit signal as a pure causal OHLCV function (no freqtrade /
   talib / pandas / execution import) — `rob382_signal_<key>.py`.
2. Simulate non-overlapping long trades on real Binance USDⓈ-M klines (`rob382_backtest.py`,
   entry at bar close, SL-first conservative exit).
3. **Stage 1 — cost-blind GROSS screen** (`discovery.screen`, triviality floor 0.5 bps +
   OOS-gross sign).
4. **Stage 2 — baseline-aware cost/OOS gate** (`validated_gate.evaluate_gate` at the frozen
   taker fee, vs REAL micro-breakout + seeded-random baselines): walk-forward OOS net,
   profit-factor, param-stability, bootstrap CI, beats-baselines.
5. **Decisive-survivor bar:** `gate == validated AND t_oos_gross ≥ 2.0 AND oos_net@taker > 0
   AND beats both baselines`. Only this would justify a follow-up backtest issue.

Cost model (frozen, `frozen_config.py`, `config_hash=8f02dffd…`): taker **4.0 bps/leg**
(= 8 bps round-trip), maker 2.0, economic-triviality floor 0.5 bps, target_t 2.0. We also
report net at the **retail REF fee (10 bps/leg = 20 bps round-trip)** to make cost sensitivity
explicit.

## Candidate selection (read-only, via gstack `/browse` + read-only WebFetch)

From the strat.ninja monthly leaderboard (read 2026-05-31), prioritizing diversity from what
we have already disproven, unbiased (no lookahead flag), no tight-trailing, 1x (spot = 1x),
and **explicitly EXCLUDING ICT** (already covered by `ict_signal.py` / `strategy_ict.py`):

| key | strat.ninja | shape (new vs our disproven set) | native TF | source (canonical, public) |
|---|---|---|---|---|
| `ichi` | ichi_5m | Ichimoku cloud + EMA fan-magnitude (trend-follow) | 5m | PeetCrypto/freqtrade-stuff `IchisV1.py` |
| `elliot` | ElliotV5_SMA | EWO (EMA50−EMA200) + SMA-offset dip + 1h uptrend | 5m | PeetCrypto/freqtrade-stuff `ElliotV7.py` † |
| `vwap` | VWAPStrategy_1478 | rolling-VWAP band dip + CTI + multi-RSI (volume) | 5m | PeetCrypto/freqtrade-stuff `VWAP.py` |
| `cluc` | ClucHAnix_3 | Heikin-Ashi + Bollinger squeeze dip + 1h ROCR | 1m | PeetCrypto/freqtrade-stuff `ClucHAnix.py` |

† `elliot` ported from `ElliotV7` (direct public successor with an IDENTICAL EWO + SMA-offset
entry shape); the exact `ElliotV5_SMA` hyperopt params were not publicly retrievable, so V7's
published defaults are used. The contrast row is the strat.ninja `ElliotV5_SMA` entry (the
shape being falsified).

## Data (PUBLIC, gitignored, never committed)

Binance USDⓈ-M futures klines from `data.binance.vision`: **BTCUSDT, ETHUSDT, XRPUSDT,
SOLUSDT** × {1m, 5m, 1h} × **2024-01 … 2025-12** (545 MB, fetched by `fetch_rob382_data.py`).
OOS split = **2025-01-01** (in-sample 2024, OOS 2025). Buy-and-hold context over the window:
in-sample 2024 was bullish (BTC +106%, XRP +198%, ETH +30%, SOL +22% over the full window),
but **OOS 2025 was flat-to-bearish** (BTC −6%, ETH −11%, XRP −12%, SOL −34%) — so positive
OOS gross is NOT a bull-beta artifact; the dip-buy-bounce captures positive gross even in the
2025 downturn. The kill is statistical significance + power + cost, not regime beta.

## Contrast table — their in-sample SPOT score vs our OOS USDⓈ-M verdict

| candidate | their Ninja / TotProf% / Win% (spot, in-sample, NOT evidence) | our trades (OOS) | our gross bps (OOS) | our net@taker 8bps RT (OOS) | our net@retail 20bps RT (OOS) | OOS t | gate | beats BO/rnd | our verdict | decisive? |
|---|---|---|---|---|---|---|---|---|---|---|
| `ichi` | n/a (insufficient data on strat.ninja) | 830 (385) | 15.26 (13.73) | +7.26 (**+5.73**) | −4.74 (**−6.27**) | **1.19** | validated | ✓/✓ | gross_edge_AND_oos_validated | **No** (t<2, retail-neg) |
| `elliot` | 62 / +2.73% / 75.67% | 18 (5) | 129.7 (140.4) | +121.7 (+132.4) | +109.7 (+120.4) | 1.60 | insufficient_data | ✓/✓ | gross_edge_but_underpowered | **No** |
| `vwap` | 56 / **−0.70%** / 93.33% | 43 (17) | 69.2 (100.0) | +61.2 (+92.0) | +49.2 (+80.0) | 1.00 | insufficient_data | ✓/✓ | gross_edge_but_underpowered | **No** |
| `cluc` | 44 / +0.46% / 70.33% | 198 (89) | 129.9 (147.5) | +121.9 (+139.5) | +109.9 (+127.5) | 1.68 | insufficient_data | ✓/✓ | gross_edge_but_underpowered | **No** |

The headline gap is NOT "their positive vs our negative" (the simple expectation): at native
timeframe these clear the gross stage. The gap is **their confident single-month spot score vs
our finding that NONE reaches OOS statistical significance (t < 2) on USDⓈ-M, 3 of 4 are
under-powered, and the one testable strategy is net-negative at realistic retail cost.**

## Why no decisive survivor (the four kills)

1. **Statistical insignificance (the primary kill):** every candidate's OOS gross t-stat is
   below the target_t = 2.0 bar — ichi 1.19, vwap 1.00, elliot 1.60, cluc 1.68. The
   high-gross candidates (elliot/cluc, ~130 bps/trade) owe that to a few fat-tailed TP/SL
   outcomes, not a tight, repeatable edge.
2. **Under-power:** elliot (18) and vwap (43) fire too rarely on the 4-symbol USDⓈ-M universe
   for the walk-forward gate (needs ≥100 trades/fold); cluc's 198 still splits to 99/49/49.
   Only ichi (830) has the trade count to validate.
3. **Cost sensitivity:** ichi is net-positive only at the 4 bps/leg frozen taker; at the
   retail 10 bps/leg reference it is net-NEGATIVE (−6.27 bps OOS).
4. **Weak controls beaten, but weakly:** all 4 beat the micro-breakout + random-entry
   baselines, but in a mean-reverting/choppy 2025 those are low bars, and the margin is not
   significant.

## Decision

`overall_verdict = no_decisive_survivor`. **Close the strat.ninja-leaderboard line. Open NO
backtest issue.** Per ROB-382's gate, a survivor would have required `gross + t>2 OOS +
beats-baselines + net-positive at our taker`; none qualifies. This is a decisive, reusable
falsification: **external freqtrade leaderboards (even the cleaner, lookahead-filtered
strat.ninja pool), ported faithfully at their native timeframe, clear the gross stage but do
NOT survive our OOS USDⓈ-M robustness gate.**

## Faithfulness (timeframe/hold preservation — ROB-382 §2)

- **Timeframe preserved** for all four (5m / 5m / 5m / 1m) — no short-horizon coercion.
- **Exit models** (ported as the strategy's OWN mechanism, not optimized):
  - `ichi`: signal exit (close crosses below EMA(close,24)) + published hard stop −27.5% +
    24h max-hold cap. `horizon_changed_during_port = False`.
  - `elliot`: signal exit (populate_sell_trend) + hard stop −32% + 24h cap. `False`.
  - `vwap`: empty exit signal in source → exit at the PUBLISHED `minimal_roi` 2% TP / stoploss
    −15% (this IS its exit). `False`.
  - `cluc`: signal exit (fisher cluster) + hard stop −32%; custom trailing-stop and
    `minimal_roi {70:0}` time-exit NOT modeled → approximated by hard-stop + 24h max-hold.
    **`horizon_changed_during_port = True`** (exit MECHANISM approximated; timeframe + entry
    + 1h informative intact).
- Both spot-checked ports (`ichi`, `cluc`) reproduce the source's quirks exactly (ichiV1's
  HA-overwrite-but-keep-real-close; ClucHAnix's `np.nan_to_num` warmup, lookahead-safe 1h
  `rocr_1h` merge). All 17 candidate tests pass (entry-fires / entry-blocked /
  no-lookahead-truncation-invariance / runs-on-real-slice).

## Caveats

- **Exit approximation:** faithful ports of the SIGNAL logic, but freqtrade's exact ROI
  ladders / trailing-stops / custom stoploss are not bit-replicated (signal exit + published
  hard stop + max-hold). Gross numbers are indicative, not exact freqtrade-backtest replicas.
- **Bounded universe:** 4 symbols → low trade counts for the rarer-firing entries (elliot,
  vwap). A broader universe would raise power but is out of scope; "under-powered" is a
  `needs_more_data` outcome, NOT a validation.
- **`ElliotV5_SMA` substitution:** ported from the V7 successor (identical entry shape),
  see †.

## Safety evidence

No freqtrade/talib runtime import into the trader; no broker/order/watch/order-intent/approval/
trade-journal mutation; no scheduler/TaskIQ/Prefect/cron; no production DB write; no secrets;
**no raw bars or leaderboard dumps committed** (counts-only artifact). Leaderboard accessed
read-only via gstack `/browse` + read-only WebFetch. All artifacts under `research/` +
`docs/runbooks/` (outside CI lint/test scope).

## Reproduce

```bash
cd research/nautilus_scalping
# 1. fetch public klines (gitignored, ~545 MB; resumable)
uv run --no-project python fetch_rob382_data.py
# 2. wiring self-test (no network/data/modules)
uv run --no-project python run_rob382_falsification.py --self-test
# 3. tests for the 4 ports
uv run --no-project --with pytest python -m pytest tests/test_rob382_*.py -q
# 4. full falsification run -> results/rob382/rob382_falsification.v1.json (gitignored)
uv run --no-project python run_rob382_falsification.py
```

Artifact schema: `rob382_falsification.v1` (counts-only; `config_hash` pins the frozen cost
model — any tweak changes the hash, so ex-post threshold edits are detectable).
