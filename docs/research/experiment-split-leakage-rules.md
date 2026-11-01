# Experiment split and leakage rules (ROB-1429)

Status: binding for every experiment in `research/experiments/registry.json`.
Applies to: backtests, model training (boosting baseline, TFT challenger), and
forward shadow epochs. It does not change any runtime, policy or order path.

These rules exist because a result that has seen its own test data is not a
result. A manifest that breaks one of them must not be sealed, and a report
built on a broken split must not cite a registered hash.

## 1. Vocabulary

| Term | Meaning |
|---|---|
| decision time `t` | The moment a rule or model would act. Every input must be available at or before `t`. |
| `available_at` | The time a value became known to us, not the time it describes. A daily close is available after the session closes; a disclosure is available at its receipt time; a fundamentals snapshot is available when it was written. |
| session | One trading session of the experiment's sealed calendar (XKRX, XNYS, or 24/7 UTC day for crypto). All gaps and horizons are counted in sessions of that calendar. |
| label horizon `h` | The number of sessions after `t` that the label reads (for example `t+1 open → t+5 close` reads 5 sessions). |
| fold | One (train, validation) pair in the walk-forward. |
| test look | Any computation of a metric on test-period rows, including a single printed number. Each look increments the hypothesis's `k` in the registry. |

## 2. Point-in-time inputs

1. Every feature row carries `available_at`. The join takes only rows with
   `available_at ≤ t`. A row without `available_at` is excluded and counted;
   it is never assumed to be available.
2. Use only bars that had closed at `t`. A forming intraday bar is never a
   feature. Indicators (RSI, ATR, support levels) are computed from closed
   bars only.
3. Universe membership is point in time. The universe at `t` includes symbols
   that were listed at `t` and later delisted. A survivor-only universe makes
   any market-wide claim invalid; the manifest must name the universe source
   and its delisted coverage.
4. A corporate action between `t` and the horizon end without a sealed
   adjustment rule makes the outcome `UNKNOWN_OUTCOME`. Do not apply an ad hoc
   factor.
5. Missing data is `UNKNOWN`, never zero, never "no fill", never "no trigger".
   A "did not happen" result needs proof that the observation window was
   complete (ROB-1404 2B / 145020 defect).

## 3. Splits use common dates only

1. Folds are cut on calendar dates of the sealed calendar, the same dates for
   every symbol. Never split by row index, by symbol, or by random shuffle.
2. Within one market all symbols share the same fold boundaries. Across
   markets each market uses its own calendar, but the boundary dates are fixed
   in the manifest before any data is read.
3. Default scheme (plan 2026-11-01, astra rule):
   - expanding walk-forward, first fold trains on 2015–2018 and validates on
     2019; each later fold adds one year to training and validates on the next
     year, through validation year 2022;
   - 2023–2024 is the sealed test period **only if** an exposure audit shows
     no earlier experiment, report, notebook or tuning run has read it.
     Otherwise the test is a prospective seal (§6).
4. A manifest may use a different scheme only by stating it in `splits`
   before registration. Changing it later is a new manifest version.

## 4. Purge and gap at every boundary

At every boundary between train and validation, and between validation and
test:

1. **Purge**: drop every training row whose label window overlaps the next
   period. A row at `t` with horizon `h` is purged if any of sessions
   `t+1 … t+h` lies in the next period.
2. **Gap**: after the purge, also drop the last **5 sessions** before the
   boundary (the embargo). With `h > 5`, the gap is `h` sessions.
3. The purge and gap are applied to every feature that looks back as well:
   a rolling statistic that spans the boundary is computed from training-side
   rows only.
4. The manifest records `purge_sessions` and `gap_sessions`; the lint rejects
   a purge below the longest label horizon and a gap below
   max(5, longest label horizon).

## 5. Preprocessing fitted in-fold

1. Every fitted transform — scalers, winsorisation limits, imputation values,
   target encodings, feature selection, PCA, quantile bins, hyperparameters —
   is fitted on the training rows of the fold and only then applied to its
   validation rows.
2. Nothing is fitted on the full history, on validation rows or on test rows.
   A global z-score computed before splitting is a leak.
3. Hyperparameter search runs inside the walk-forward and is limited by the
   manifest's `tuning_budget`. Every configuration tried counts against the
   budget, including discarded ones.
4. The final model is refitted on train + validation of the last fold with the
   chosen configuration, then frozen (code SHA, weights hash, config hash)
   before the test is opened.

## 6. Sealed final test, opened once

1. The test period is sealed when the manifest is registered. Its rows are
   not loaded, plotted, summarised or used for feature selection before the
   frozen model is ready.
2. Opening the test is a **test look**. Before the look, append an entry to
   the hypothesis's `test_looks` in the registry and increment `k` in the same
   commit, with the report's sha256. The report cites the manifest hash
   (`Experiment-Manifest-Sha256` marker) and declares the look
   (`Experiment-Test-Look` marker set to yes); marker syntax is in
   `research/experiments/README.md`.
3. A test period is never reopened for the same hypothesis after a failed
   look. A new idea needs a new hypothesis with a new test period, usually a
   prospective seal.
4. **Prospective seal**: when no untouched historical period exists, freeze
   the model and manifest, record the freeze time, and evaluate only on
   decisions made after the freeze. The window length and end date are fixed
   at the freeze and are never extended (ROB-1411 §10, ROB-1301 Q6).
5. Interim results of a forward epoch are not a test look and are not
   reported as metrics; the report says `INTERIM — no verdict`.

## 7. Multiple testing and the cumulative k

1. `k` counts test looks for a hypothesis across all its manifest versions.
   It only grows. A report quotes the `k` at the time of its look.
2. The verdict threshold is adjusted for the hypothesis family declared in
   the manifest (`metrics.family`), and the report states `k` so a reader can
   discount repeated looks.
3. A look that is not in the registry does not exist as evidence. The CI lint
   (`scripts/ci/experiment_registry_lint.py`, run in the CI `lint` job; tests in
   `tests/ci/test_experiment_registry_lint.py`) fails a report that
   cites an unregistered hash or claims a look the registry does not count.

## 8. Checklist before registering a manifest

- [ ] calendar and boundary dates fixed; common dates for all symbols
- [ ] `purge_sessions ≥ max label horizon`, `gap_sessions ≥ max(5, max label horizon)`
- [ ] every feature has an `available_at` rule
- [ ] universe source includes delisted symbols (or the claim is limited)
- [ ] preprocessing list states "fit in-fold"
- [ ] tuning budget and seeds fixed
- [ ] test period: exposure audit result or prospective seal
- [ ] stopping rules and primary metric fixed
