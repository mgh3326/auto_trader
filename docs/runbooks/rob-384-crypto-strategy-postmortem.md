# ROB-384 — crypto strategy failure-mode postmortem + closure decision

**Status:** analysis complete 2026-05-31. Research/synthesis only — **no broker /
order / watch / order-intent / Demo / live / scheduler side effects**, no new
strategy survey / hyperopt / parameter sweep / campaign, no raw market-data
commit, no secrets.

This is the closure capstone over five prior negative results
(ROB-320 / 342 / 353 / 382 / 383). It re-parses existing counts-only artifacts,
assigns a deterministic failure-mode taxonomy, recomputes a fee grid, and emits
a single A/B/C closure decision. It is **not** new strategy discovery.

- **Code:** `research/nautilus_scalping/external_strategy_sieve/postmortem/`
  (`fees.py`, `evidence.py`, `gatereport_io.py`, `taxonomy.py`, `residual.py`,
  `runner.py`, `tests/`). Pure stdlib; runs under `uv run --no-project`.
- **Committed counts-only CSV:** `docs/runbooks/rob-384-crypto-strategy-postmortem.csv`.
- **Regenerable JSON (gitignored):** `<artifact_root>/postmortem/rob384_postmortem.v1.json`.
- **Reproduce:**
  ```bash
  cd research/nautilus_scalping
  # stage the four source artifacts under the artifact root (results/ or
  # $AUTO_TRADER_RESEARCH_ARTIFACT_ROOT): rob320/meanrev.json,
  # discovery/rob383/phase3_validation.json, rob382/rob382_falsification.v1.json,
  # rob353/rob351_campaign.v1.json
  uv run --no-project python -m external_strategy_sieve.postmortem.runner          # summary
  uv run --no-project python -m external_strategy_sieve.postmortem.runner --emit    # CSV + JSON
  uv run --no-project --with pytest python -m pytest external_strategy_sieve/postmortem/tests/ -q
  ```

---

## 0. Closure decision

> **A. The crypto public / OHLCV short-horizon strategy line is CLOSED.**

Of 14 candidates across the five issues: **6 `closed`, 8 `not_worth_pursuing`,
0 `maybe_worth_feasibility`, 0 candidates worth a pre-registered re-test.**

The decision is derived deterministically (`residual.closure_decision`), not
asserted:

- The verdict is **B** only if some candidate is a *positive, stable,
  fee-only-killed* edge whose maker-fill / execution-realism rescue is genuinely
  untested. No candidate qualifies. The closest misses are `freqtrade_bbrsi_naive`
  and `tv_chandelier_exit` (gross +10.3 / +10.5 bps, multi-fold, net-positive at
  the 4 bps demo taker, net-negative only at retail) — they are excluded only
  because ROB-383 classed them `shadow_candidate` (signal-only, "NOT new alpha")
  and **carry no OOS t-stat at all**, so their "stability" is the weak
  >100-trades proxy, not the OOS-t ≥ 2 the B definition intends. See §7 for the
  full disclosure. `meanrev_zscore_fade` does not qualify either: its gross edge
  is below the triviality floor (next point).
- The verdict is **C** only if exactly one gross-positive, baseline-beating,
  not-yet-OOS-confirmed candidate was left *open* by its source. None was: every
  source explicitly closed its own candidates (ROB-382 `no_decisive_survivor` →
  "open NO backtest issue"; ROB-383 reject/shadow/research classes; ROB-353
  reject for all families).
- Therefore **A**.

**The dominant failure mode is gross-insufficiency, not cost.** **6 of 14**
candidates have no economically meaningful gross edge at all: **4** are outright
`gross_zero` (gross ≤ 0: `tv_range_filter` and all three ROB-353 families), and
two more (`meanrev` +0.16 bps, ROB-342 sweep reversal +0.44 bps) sit below the
0.5 bps triviality floor. The remaining gross-positive candidates are single-fold
artifacts or underpowered ports. Execution realism
(maker-fill) can only rescue a genuinely *cost-dominated* edge — and for the one
candidate with that historical framing (meanrev) the maker/limit-fill rescue was
already tried in **ROB-324 / PR #984** and came back `not_validated` (missed-fill
+ adverse-selection; documented in the ROB-342 issue description, not in the
re-parsed meanrev artifact). The only filed
non-strategy thread, **ROB-343** (execution-realism harness), is deferred-Low and
is **not** a reason to keep searching: it targets cost, which binds for a small
minority of candidates whose maker-fill variant is already exhausted.

This joins the consistent negative book: ROB-316 / 320 / 324 / 339 / 342 / 349 /
351 / 353 / 382 / 383.

---

## 1. Premise refinement (important)

The issue assumed **ROB-342 / 353 / 382 are all documented-only** (no local JSON)
and only ROB-320 / 383 are re-parsable. **That is not the case.** Local result
JSONs exist for ROB-382 and ROB-353 as well, so this postmortem re-parses **four**
issues and hand-curates only **one** — strictly fewer hand-typed numbers, which
is the integrity-preferred path the issue itself asks for (reparsed over
documented):

| Issue | Source artifact | Schema | Provenance |
|---|---|---|---|
| ROB-320 | `rob320/meanrev.json` | `validated_signal_gate.v1` | **reparsed** |
| ROB-383 | `discovery/rob383/phase3_validation.json` | `validated_signal_gate.v1` (×5) | **reparsed** |
| ROB-382 | `rob382/rob382_falsification.v1.json` | `rob382_falsification.v1` (×4) | **reparsed** |
| ROB-353 | `rob353/rob351_campaign.v1.json` | `rob351_campaign.v1` (×3) | **reparsed** |
| ROB-342 | — (no local artifact) | documented | **documented** |

### Integrity flag — ROB-342 memory-only detail is NOT counted

ROB-342 (archived, 0 Linear comments, no committed design doc) is the only
documented row. The **only citable** ROB-342 evidence is its own issue
description, which quotes the ROB-339 smoke: best gross edge **+0.44 bps**
(BTCUSDT sweep reversal) / **+0.28 bps** (XRPUSDT sweep/time-of-day) against a
**6–8 bps** cost hurdle, all 5 families `screened_out`. The stronger closure
detail recalled elsewhere ("negative even at 0 bps, BTC+XRP n≥263,
1yr regime-conditioned OOS") **could not be traced to a live source on
2026-05-31** and is therefore **excluded from all quantitative fields** and
flagged `memory_only_uncited` in the JSON. It does not change the verdict: the
same conclusion (short-horizon crypto reversal/trend is gross-negative) is
established independently by the **re-parsed ROB-353** result.

---

## 2. Per-candidate evidence + fee grid

Per-trade bps at the sieve convention (notional 1000). Fee is **per leg**; the
grid is an exact linear interpolation between the gross run (fee 0, identical to
the `zero_fee` fold) and the reference run (10 bps), verified against each
artifact's own recorded points (e.g. ROB-382 ichi's recorded frozen-taker 4 bps
value 7.258 = the interpolated value; ROB-383's `fee_sweep_net_pnl` is exactly
linear). Counts-only mirror of the committed CSV.

| Issue | Candidate | Family | gross | net@2 | net@4 | net@7.5 | net@10 | trades | OOS t | source verdict |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|---|
| ROB-320 | meanrev_zscore_fade | MR z-fade | **+0.16** | −0.40 | −0.97 | −1.95 | **−2.66** | 789 | — | not_validated |
| ROB-383 | freqtrade_supertrend | trend (Supertrend) | **+4.37** | +0.37 | −3.63 | −10.63 | **−15.63** | 1 828 | — | reject |
| ROB-383 | freqtrade_bbrsi_naive | MR (BB+RSI) | **+10.32** | +6.32 | +2.32 | −4.68 | **−9.68** | 6 903 | — | shadow |
| ROB-383 | tv_squeeze_momentum | vol (TTM squeeze) | **+13.60** | +9.60 | +5.60 | −1.40 | **−6.40** | 2 230 | — | research |
| ROB-383 | tv_range_filter | trend (range filter) | **−5.52** | −9.52 | −13.52 | −20.52 | **−25.52** | 10 981 | — | reject |
| ROB-383 | tv_chandelier_exit | trend (chandelier) | **+10.52** | +6.52 | +2.52 | −4.48 | **−9.48** | 3 914 | — | shadow |
| ROB-382 | ichi | ichimoku trend | **+15.26** | +11.26 | +7.26 | +0.26 | **−4.74** | 830 | **1.19** | validated/not-decisive |
| ROB-382 | elliot | EWO SMA-offset MR | **+129.73** | +125.73 | +121.73 | +114.73 | **+109.73** | **18** | 1.60 | underpowered |
| ROB-382 | vwap | VWAP band dip | **+69.17** | +65.17 | +61.17 | +54.17 | **+49.17** | **43** | 1.00 | underpowered |
| ROB-382 | cluc | HA BB-squeeze MR | **+129.85** | +125.85 | +121.85 | +114.85 | **+109.85** | 198 | 1.68 | underpowered |
| ROB-353 | family1_breakout_continuation | breakout | **−70.99** | _moot_ | _moot_ | _moot_ | _moot_ | 1 366 | — | screened_out |
| ROB-353 | family2_ts_trend_basket | TS trend | **−27.53** | _moot_ | _moot_ | _moot_ | _moot_ | 58 | — | screened_out |
| ROB-353 | family3_xs_momentum | XS momentum | **−39.38** | _moot_ | _moot_ | _moot_ | _moot_ | 172 | — | screened_out |
| ROB-342 | short_horizon_sweep_reversal | sweep reversal | **+0.44** | _n/p_ | _n/p_ | _n/p_ | _n/p_ | — | — | screened_out (documented) |

_moot_ = gross ≤ 0, so net at any fee is irrelevant (no edge to tax). _n/p_ = net
grid not published in the documented source. The ROB-382 OOS t-stat is
`our_t_stat_oos_gross` (target 2.0); none clears it.

---

## 3. Failure-mode taxonomy (deterministic, tested)

`taxonomy.assign_failure_modes` is a pure function of the recorded numbers
(`tests/test_taxonomy.py`).

| Candidate | Failure modes |
|---|---|
| meanrev_zscore_fade | `cost_dominated` (gross +0.16 bps is also below the 0.5 bps triviality floor) |
| freqtrade_supertrend | `cost_dominated`, `single_fold_only` |
| freqtrade_bbrsi_naive | `cost_dominated`, `fee_fragile`, `license_shadow_only` |
| tv_squeeze_momentum | `cost_dominated`, `fee_fragile`, `single_fold_only`, `source_unfaithful` |
| tv_range_filter | `gross_zero` |
| tv_chandelier_exit | `cost_dominated`, `fee_fragile`, `license_shadow_only` |
| ichi | `cost_dominated`, `fee_fragile` (OOS t 1.19 < 2 → not decisive) |
| elliot / vwap / cluc | `single_fold_only` (underpowered: n = 18 / 43 / 198, OOS t < 2) |
| ROB-353 family1 / family2 / family3 | `gross_zero` (`cost_binding_screen=false` — fees are not the bottleneck) |
| ROB-342 sweep reversal | `cost_dominated` (gross +0.44 bps ≪ 6–8 bps cost hurdle) |

**Distribution:** `gross_zero` 4 · `cost_dominated` 7 · `fee_fragile` 4 ·
`single_fold_only` 5 · `source_unfaithful` 1 · `license_shadow_only` 2. The
`regime_bound`, `listing_artifact`, and `implementation_blocked` codes are not
auto-assigned (no determinate recorded signal); ROB-353 specifically used the
**survivorship-corrected** PIT panel, so its negatives are *not* a
`listing_artifact`.

---

## 4. Baseline outperformance (where data exists)

| Source | Baselines recorded | Result |
|---|---|---|
| ROB-320 | micro-breakout, random-entry (same-turnover) | meanrev beats both (less-negative) but is still net-negative |
| ROB-383 | micro-breakout, random-entry per candidate | the shadows/research candidates beat the random/momentum baselines at the *demo taker* fee, but all go net-negative by retail |
| ROB-382 | `beats_micro_breakout_baseline`, `beats_random_baseline` | all 4 beat both on gross, **none** is a decisive survivor (gross + t>2 OOS + net-positive at frozen taker) |
| ROB-353 | BTC buy-&-hold **+35 938 bps (+359%)** over window; cash 0 bps | every family is far below buy-&-hold and below cash — a passive long-BTC or cash stance dominates all three |
| ROB-342 | not published | absence ≠ pass — recorded as unknown, not as a beat |

Beating a same-turnover random or micro-breakout baseline is necessary but not
sufficient: the candidates that clear it (ROB-382) still fail the decisive bar,
and none beats the trivial buy-&-hold / cash baseline net of retail cost.

---

## 5. Is `tv_squeeze_momentum`'s gross-positive single-fold result an artifact?

**Yes — it is a single-fold (regime/horizon) artifact, not alpha.** Its
walk-forward net PnL by fold is:

| fold | net PnL |
|---|--:|
| train | **−185** |
| val | **−834** |
| oos | **+2 269** |

The entire positive result lives in the single OOS fold; train and val are both
negative. The sieve flagged exactly this ("edge appears in only one fold") and
classed it `research_candidate`, not `demo_ready`. `freqtrade_supertrend` shows
the identical pattern (train −2 608, val −907, oos +2 852). Concentration of the
whole edge in one contiguous out-of-sample window is the signature of a regime
coincidence, not a stable signal — and `tv_squeeze_momentum` additionally carries
a `source_unfaithful` caveat (its momentum leg was simplified from the LazyBear
linreg to a close-SMA in the clean-room port). It is `not_worth_pursuing`.

---

## 6. Limits of the public-strategy source universe

These bound how much the negative result can generalize:

- **Publication / leaderboard bias.** strat.ninja and freqtrade leaderboards
  surface strategies by in-sample SPOT performance; their headline numbers are
  contrast-only and not evidence (ROB-382 keeps them in a separate `their_*`
  column). Survivors of a public leaderboard are pre-selected for in-sample
  overfit, which is exactly what dies in OOS.
- **License / DCA / custom-exit dependence.** Several public bots embed DCA,
  martingale, grid, or bespoke ROI/exit ladders. The clean-room ports model the
  *signal* only; where the published edge depends on an averaging/exit overlay we
  do not (and should not) reproduce, the signal alone underperforms — but that
  overlay is also where the tail risk lives.
- **Clean-room signal-extraction loss.** Re-implementing an indicator from a
  description (not the original code) loses fidelity (`source_unfaithful`, e.g.
  squeeze momentum). This *understates* a faithful strategy's edge — but a signal
  this fragile to a minor indicator substitution is not a robust edge either.
- **Spot→USDⓈ-M transfer.** Leaderboard numbers are usually spot; re-deriving on
  Binance USDⓈ-M futures with the frozen cost model removes the spot-fee and
  spot-microstructure tailwinds the published numbers enjoyed.

None of these limits points to a missed winner: the failures are dominated by
gross-insufficiency and single-fold concentration, which a more faithful port or
a broader survey would not reverse.

---

## 7. Residual hypothesis map

| Line | Status | Why |
|---|---|---|
| Ultra-short scalping (ROB-316/320/339) | `closed` | net-negative purely on fees; gross edge ≤ ~0.4 bps ≪ cost |
| Mean-reversion z-fade (ROB-320) | `closed` | gross +0.16 bps is below the 0.5 bps triviality floor (the load-bearing close reason); maker/limit-fill rescue additionally tested negative in ROB-324 (per the ROB-342 issue description) |
| Short-horizon reversal/continuation (ROB-342) | `closed` | gross +0.44 bps ≪ 6–8 bps hurdle; subsumed by ROB-353 |
| Generic trend / momentum / XS families (ROB-353) | `closed` | gross-negative OOS; `cost_binding_screen=false` — no edge before fees |
| External freqtrade/TradingView signals (ROB-383) | `not_worth_pursuing` | range-filter gross-negative; supertrend/squeeze single-fold; bbrsi/chandelier fee-fragile shadows (NOT new alpha) |
| External leaderboard signals (ROB-382) | `not_worth_pursuing` | `no_decisive_survivor`; ichi fee-fragile + OOS t 1.19; elliot/vwap/cluc underpowered |
| Execution realism / maker-fill (ROB-320/343) | **non-strategy feasibility, deferred-Low** | the only cost-dominated stable candidate (meanrev) already failed maker-fill in ROB-324; cost binds for a small minority |

**No line is `maybe_worth_feasibility`.** The maker-fill thread that option B
would point to is already spent for the one candidate it could have helped.

**Disclosure — the closest misses to B.** `freqtrade_bbrsi_naive` and
`tv_chandelier_exit` literally match the *shape* of a maker-fill feasibility
candidate (gross +10.3 / +10.5 bps, multi-fold, net-positive at the 4 bps demo
taker, net-negative only at retail, maker-fill never tested). They are kept out
of `maybe_worth_feasibility` by a single, defensible disqualifier — they were
classed `shadow_candidate` ("signal-only, NOT new alpha") by ROB-383 and **carry
no recorded OOS t-stat**, so their "stable edge" is only the weak >100-trades
proxy, not the OOS-t ≥ 2 stability the definition intends (unlike ichi, whose
recorded OOS t = 1.19 explicitly fails). Dropping that one disqualifier would flip
both to `maybe_worth_feasibility` → verdict **B**. The verdict A is therefore
robust but *conditional* on treating ROB-383's shadow classification + the
absence of OOS-t validation as sufficient to decline them. If an operator wanted
to be maximally conservative, the single highest-value follow-up would be a
pre-registered, no-tuning OOS-t test of bbrsi/chandelier at maker fees — but
ROB-383 already adjudicated them not-alpha, so this is explicitly **not**
recommended here.

---

## 8. Acceptance criteria

- [x] Per-candidate table (gross/net/sample/fold/verdict) across all five issues with `source` provenance — §2, CSV.
- [x] Fee grid 0/2/4/7.5/10 bps for re-parsable candidates; documented as-recorded for the rest — §2.
- [x] `failure_mode` taxonomy assigned (deterministic, tested) — §3, `tests/test_taxonomy.py`.
- [x] Baseline-outperformance flags where data exists; absence ≠ pass — §4.
- [x] squeeze_momentum single-fold regime/horizon analysis — §5.
- [x] Public-source limitations section — §6.
- [x] Residual hypothesis map + single explicit closure decision (A) — §7, §0.
- [x] Counts-only CSV committed; JSON in gitignored results; no raw data / secrets — runner.
- [x] Tests for new parsing/fee/taxonomy/residual/runner code pass locally — 36 passed.

## 9. Non-goals (restated)

No new winner is claimed. Any Demo activation, new campaign, or parameter tuning
is explicitly out of scope; if ever justified it belongs in a separate
operator-approved issue. ROB-343 (execution-realism harness) remains
deferred-Low and is **not** reopened by this postmortem.
