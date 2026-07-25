# ROB-847 Honest Offline Gate Design

## Purpose

ROB-847 makes offline research evidence causally executable and promotion-safe.
It removes current-close/same-close look-ahead, prevents evaluation-window
overlap, requires point-in-time evidence, accounts for every trial through the
ROB-846 immutable registry, and keeps sealed OOS data outside parameter
selection.

The change does not expand strategy search, reopen ROB-384 candidates, or touch
broker, paper, live, Binance Demo execution, or ledger code.

## Existing boundaries

The implementation reuses these ROB-846 contracts without copying them:

- `StrategyExperimentIdentity`, `BacktestTrialRequest`, and
  `PromotionLinkRequest` from `app/schemas/research_backtest.py`;
- `register_experiment`, `record_trial`, `get_trial_accounting`, and
  `link_promotion_candidate` from
  `app/services/strategy_experiment_registry.py`;
- `research.strategy_experiments`, `research.backtest_runs`, and
  `research.promotion_candidates`;
- typed canonical AST hashing and append-only trial accounting.

No migration is needed. A run can have only one promotion-candidate row because
`research.promotion_candidates.backtest_run_id` is already unique. That row is
the durable one-time-finalize seal. Exact experiment/config/data hashes and the
gate artifact payload fit the existing promotion-candidate identity columns and
JSON `thresholds`/`metrics` columns.

The stdlib-only canonical hash, frozen config, trial-evidence parser, and pure
gate live in the small `research_contracts` package, which is explicitly
included beside `app` in the production wheel. The historical
`research/nautilus_scalping` module paths are compatibility re-exports only;
the application never imports the unpackaged research tree. A clean built-wheel
smoke test imports the app schema/finalizer and verifies the pinned canonical
digest without packaging the larger research scripts or tests.

## Root causes

1. `backtest/prepare.py` calls a strategy with history through bar `t`, then
   immediately executes returned signals against bar `t` close. A signal can
   therefore profit from a price it could not have traded after observing it.
2. The current fold 4 validation window ends on 2026-03-22, while the named test
   window begins on 2026-02-01. The same observations can influence both model
   selection and final evaluation.
3. Existing validated-gate code calculates OOS parameter ranks while deciding
   whether the validation-selected parameter is stable. That lets sealed OOS
   values influence a selection-adjacent decision.
4. The legacy experiment loop records keep/revert/crash outcomes in TSV and git
   history. Those records neither provide complete trial accounting nor exact
   immutable experiment/config/data identity.
5. Legacy ingestion may compute a positive gate result without experiment
   identity. ROB-846 already prevents direct promotion linkage, but ROB-847 must
   make the missing honest-gate artifact an explicit, stable non-promotable
   reason as well.

## Architecture

### Causal backtest execution

`backtest/prepare.py` retains the strategy-facing `on_bar` interface. The engine
executes pending signals from the preceding bar before calling the strategy for
the current bar. Signals emitted after observing bar `t` are stored until the
next chronological bar.

The only default executable price is the next bar open. An explicitly supplied
next executable quote may replace that open only through the same validated
fill-price boundary. A fill price must be finite and strictly positive. Missing
symbol bars, missing opens, malformed opens, and signals emitted on the final
bar remain unfilled. They never fall back to the current or next close.

The frozen cost definition applies fee, half-spread, and slippage at fill time:

- buy price = next executable price multiplied by
  `1 + (half_spread_bps + slippage_bps) / 10_000`;
- sell price = next executable price multiplied by
  `1 - (half_spread_bps + slippage_bps) / 10_000`;
- fee is charged on executed notional using the frozen fee rate.

Trade logs retain both signal timestamp and fill timestamp so causal ordering is
auditable.

### Evaluation-window admission

The stdlib-only `research_contracts.evaluation_windows` module owns one
immutable train, validation, sealed-OOS, and CV-fold schedule. Both
`CampaignConfig` identity and `backtest/prepare.py` consume that authority;
`prepare.SPLITS` and `prepare.CV_FOLDS` are derived views, not independent date
definitions. A pure window validator accepts named closed intervals. It checks
valid ordering within each interval and pairwise intersection across train,
every validation fold, and sealed OOS. Any intersection returns the stable
reason `overlapping_evaluation_windows` plus both window names.

The historical fold-4/test pair remains in a regression fixture to preserve the
bug evidence. The production default changes fold 4 to end before sealed OOS
starts. Warmup data may precede a validation interval, but scored observations
must not overlap another evaluation interval.

### Frozen configuration and identity

The honest-gate configuration contains all definitions that may change a
promotion decision:

- DSR probability threshold and minimum sample count;
- CSCV slice count and PBO maximum;
- FDR alpha and economic minimum edge;
- cash, BTC/ETH equal-weight, and same-turnover random baseline definitions;
- random-baseline seed and repetition count;
- fee, spread, slippage, and cost-stress multipliers;
- maximum-drawdown target;
- split/window definitions and PIT validation policy.

Canonical serialization of that complete structure produces the frozen config
hash. The exact evaluation-window structure is also embedded in the policy
identity, so experiments with different windows cannot enter the same campaign
query. Benchmark, cost, and MDD definitions also remain explicit ROB-846
identity components. A changed definition cannot reuse the prior exact identity
link: promotion fails unless the caller registers and uses the resulting new
experiment identity/version.

### Selection and sealed OOS

The registered `autoresearch` runner at the frozen `1d` timeframe persists every
evaluated candidate's finite canonical CV score in `honest_trial.v3` evidence
together with immutable producer/version provenance and the declared Sharpe,
p-value, and `canonical_cv_score` selection methods. Every statistical and cost value
must be a finite native JSON number (`int` or `float`, excluding booleans);
numeric strings, `Decimal`, and other coercible objects fail closed. Finalization
requires v3 evidence and exact equality between runner, timeframe,
schema/producer/version, and all three method identities in the frozen config.
Legacy v1/v2 rows remain parseable for compatibility but have no final-gate
producer authority and fail closed.
The methods are part of both the frozen config hash and ROB-846 policy identity.

Finalization treats each immutable `experiment_id` as a distinct candidate,
even when multiple experiments share the same parameter hash. It requires
exactly one completed/rejected evidence row for every visible candidate and
retains both the evidence run id and parameter-hash provenance. The target
`backtest_run_id` must itself be that candidate's evaluated evidence row and its
experiment must be the server-selected winner; a crashed target cannot borrow a
completed sibling run. The legacy caller-supplied `SelectionResult` remains an
input for compatibility, but is accepted only when it exactly matches that
server reconstruction; it has no decision or artifact authority. Duplicate or
missing candidate evidence, non-finite scores, score ties, or a method mismatch
are non-promotable. Selection types and signatures contain no OOS field.

Sealed OOS observations are persisted first through the dedicated internal
writer as an append-only `ResearchBacktestRun` artifact. The row has pinned
runner/timeframe/path/status metadata and an exact JSON-native payload binding
experiment, config, dataset, sealed window, returns, and metrics to its canonical
hash. The metrics contract requires both net return and a finite, non-negative
maximum-drawdown magnitude. `finalize` accepts only the opaque artifact row id;
it reloads and verifies all metadata, identity, payload, and hash rather than
accepting raw OOS values or a caller-selected drawdown.
Finalize performs these steps once:

1. reject an existing promotion candidate for the run;
2. validate exact experiment, frozen-config, and dataset-manifest hashes;
3. transaction-lock and claim the opaque sealed-OOS artifact, rejecting reuse;
4. validate the PIT manifest and information cutoff;
5. read total and outcome counts from ROB-846 trial accounting, excluding the
   artifact control row;
6. calculate DSR, PBO, FDR, fold/OOS metrics, baselines, cost stress, and MDD;
7. produce and hash the canonical gate artifact;
8. call `link_promotion_candidate` with exact hashes and the artifact evidence.

The unique promotion-candidate constraint makes concurrent or repeated finalize
calls fail closed. No API returns sealed OOS observations to the selection
layer. Changing only sealed OOS values therefore cannot change the selected
parameter.

## Statistical definitions

### Deflated Sharpe Ratio

Returns are ordered, finite period returns. For `T` observations with sample
Sharpe `SR`, sample skewness `gamma3`, and non-excess kurtosis `gamma4`, the
probabilistic Sharpe statistic against benchmark `SR0` is

```text
z = (SR - SR0) * sqrt(T - 1)
    / sqrt(1 - gamma3 * SR + ((gamma4 - 1) / 4) * SR^2)
PSR = Phi(z)
```

For DSR, `SR0` is the expected maximum Sharpe among `N` trials using the Bailey
and López de Prado approximation:

```text
SR0 = sigma_SR * ((1 - EulerGamma) * Phi^-1(1 - 1/N)
                  + EulerGamma * Phi^-1(1 - 1/(N * e)))
```

`N` is the registry's total trial count, never a caller-provided value.
`sigma_SR` is the sample standard deviation of completed-trial Sharpe values.
One trial uses a zero multiple-testing benchmark. Fewer than the configured
return observations, fewer than two finite completed-trial Sharpes when
`N > 1`, zero/non-finite trial Sharpe dispersion, zero return variance, a
non-positive denominator, or any non-finite input yields a stable
non-promotable reason rather than a numeric pass.

### Probability of Backtest Overfitting

PBO uses Combinatorially Symmetric Cross-Validation. A finite matrix contains
candidate returns by chronological slice. The configured even number `S` of
slices is split through every combination of `S/2` in-sample slices. For each
combination, the candidate with the highest in-sample score is located in the
out-of-sample ranking. Its relative rank `omega` is mapped to
`logit(omega) = log(omega / (1 - omega))`. PBO is the fraction of combinations
whose logit is less than or equal to zero.

Fewer than two candidates, an odd or insufficient slice count, empty slices,
tied/non-finite scores that prevent deterministic ranking, or no valid
combinations yields a stable non-promotable reason.

### False-discovery rate and economic edge

Each completed candidate trial supplies a finite one-sided p-value for its
predeclared edge metric. Benjamini-Hochberg sorts `m` p-values and finds the
largest rank `k` satisfying `p[k] <= alpha * k / m`. A candidate passes FDR only
when its p-value is in the rejected set. It must independently exceed the frozen
economic minimum edge after normal costs and cost stress.

Missing p-values, values outside `[0, 1]`, non-finite values, no hypotheses, or
an edge below the frozen floor produce stable non-promotable reasons.

## PIT evidence

Promotion requires a manifest hash, manifest creation/as-of timestamp,
maximum included observation timestamp, and timezone-aware
`information_cutoff`. The supplied manifest hash must equal the registered
experiment's dataset-manifest hash. The manifest timestamp and every included
observation timestamp must be less than or equal to the cutoff. A missing hash,
missing cutoff, naive timestamp, future manifest timestamp, future observation,
or mismatch produces a stable reason code and cannot be promoted.

## Gate artifact

The canonical artifact records:

- schema version, experiment/run/config/data hashes, the claimed sealed-OOS
  artifact id, the server-derived
  selected experiment key, full validation ranking/score map,
  experiment-to-parameter-hash provenance, and information cutoff;
- total trial count and zero-filled completed/rejected/crashed/timeout counts;
- DSR inputs/result, PBO inputs/result, and FDR decision;
- economic edge, train/validation/OOS fold metrics, and exact window evidence;
- frozen cash, BTC/ETH equal-weight, and seeded same-turnover random baselines;
- base-cost and stressed-cost metrics;
- configured MDD target and the hash-bound sealed-OOS observed MDD;
- PIT evidence, promotable boolean, and sorted stable reason codes.

The builder constructs one exact JSON-native payload: timestamps are UTC ISO
strings, sequences are lists, and numeric evidence is a finite native `int` or
`float`. That same payload is directly JSON-serializable, canonically hashed,
and persisted; a PostgreSQL JSONB round trip rehashes identically. Non-finite or
coercible non-native numeric values are rejected before serialization.

## Trial lifecycle

Registered experiment execution records exactly one terminal ROB-846 trial:

- successful evaluation: `completed`;
- admission, PIT, or declared-policy rejection: `rejected`;
- process or parsing failure: `crashed`;
- subprocess timeout: `timeout`.

The idempotency key is stable per invocation and the persisted runner enum is
the <=16-character `autoresearch`. Duplicate or concurrent replay returns the
original row through `record_trial`. Completed and rejected v3 evidence binds
the actual canonical CV score as `validation_score`; rejected, crashed, and
timeout rows are committed before any legacy strategy git revert. TSV
keep/revert/crash records remain exploratory diagnostics and have no promotion
authority.

After registration, subprocess launch failures and run-log write failures are
normalized through the same `crashed` path. The runner attempts the durable
terminal row first, then independently attempts strategy revert and the
exploratory TSV audit. A failure in one step does not suppress the later cleanup
attempts; one error propagates directly and multiple errors are preserved as an
exception group.

An identity-less legacy invocation cannot create registered evidence. It may
continue only as explicitly `non_promotable`, with no honest gate artifact and
no eligible promotion candidate.

## Failure normalization

Reason codes are lowercase snake case, deterministic, de-duplicated, and sorted.
The exhaustive implemented window, trial-evidence, artifact, and finalization
codes are:

- window admission: `invalid_evaluation_window`,
  `overlapping_evaluation_windows`;
- PIT and campaign cutoff: `missing_information_cutoff`,
  `invalid_information_cutoff`, `invalid_pit_evidence`,
  `information_cutoff_mismatch`,
  `campaign_information_cutoff_mismatch`, `missing_pit_manifest`,
  `pit_manifest_after_cutoff`, `pit_observation_after_cutoff`,
  `pit_manifest_hash_mismatch`;
- canonical trial evidence: `missing_candidate_trial_evidence`,
  `duplicate_candidate_trial_evidence`, `invalid_trial_evidence`,
  `non_finite_trial_evidence`, `invalid_trial_p_value`,
  `insufficient_trial_sample`, `trial_parameter_key_mismatch`,
  `trial_provenance_mismatch`, `trial_producer_mismatch`,
  `missing_selection_evidence`,
  `selection_method_mismatch`, `trial_statistic_method_mismatch`;
- exact campaign/selection universe: `selection_trial_universe_mismatch`,
  `pbo_trial_universe_mismatch`, `selected_trial_mismatch`,
  `invalid_selection_evidence`, `selection_ranking_mismatch`,
  `selection_evidence_mismatch`, `ambiguous_selection_score`,
  `target_trial_not_evaluated`, `target_trial_evidence_mismatch`;
- DSR: `insufficient_dsr_sample`, `zero_dsr_variance`,
  `non_finite_dsr_input`, `dsr_below_threshold`;
- PBO: `insufficient_pbo_sample`, `invalid_pbo_slices`,
  `non_finite_pbo_input`, `ambiguous_pbo_ranking`,
  `pbo_above_threshold`;
- FDR and economic edge: `missing_fdr_evidence`,
  `non_finite_fdr_input`, `invalid_fdr_p_value`, `fdr_not_significant`,
  `economic_edge_below_minimum`;
- baselines, costs, and MDD: `missing_required_baseline`,
  `baseline_provenance_mismatch`, `baseline_not_beaten`,
  `invalid_evidence_mapping`, `invalid_fold_metrics`,
  `execution_cost_mismatch`, `random_baseline_provenance_mismatch`,
  `cost_stress_provenance_mismatch`, `cost_stress_failed`,
  `cost_stress_baseline_mismatch`, `mdd_target_exceeded`;
- immutable identity and one-time finalization: `missing_experiment_identity`,
  `frozen_config_hash_mismatch`, `policy_identity_mismatch`,
  `benchmark_identity_mismatch`, `cost_identity_mismatch`,
  `mdd_identity_mismatch`, `promotion_hash_mismatch`,
  `invalid_sealed_oos_artifact`, `sealed_oos_artifact_identity_mismatch`,
  `sealed_oos_artifact_conflict`, `sealed_oos_artifact_already_used`,
  `sealed_oos_already_finalized`.

A static contract test extracts reason literals from the implementation and
requires this list to match exactly, so adding or removing a stable code cannot
silently leave the design document stale.

Unexpected statistical or evidence inputs fail closed; they do not become
exceptions that accidentally bypass the gate.

## Explicit residual trust boundaries

The finalizer still receives the PBO return matrix as external sealed evidence.
It enforces the exact registered candidate-key universe, finite CSCV inputs,
deterministic ranking, and the one-time seal, but ROB-846 has no immutable
per-candidate return-series artifact from which the matrix can be reconstructed.
Moving that value authority behind a persisted producer contract requires a
separate schema/ingestion change.

The generic ROB-846 record_trial primitive remains an internal trust boundary:
it does not itself authorize or attest the caller as autoresearch. The final
gate therefore requires the frozen row runner/timeframe and v3 producer fields,
while the dedicated sealed-OOS writer verifies exact replay semantics. Neither
primitive is exposed here as an external mutation API. Production promotion
must remain disabled until the approved autoresearch and sealed-OOS producers
are wired through an authorized internal execution path.

ROB-846 also has no campaign id or predeclared immutable candidate set. The gate
therefore uses every comparable experiment visible at finalization time. A
candidate registered after an eligible artifact (or deliberately withheld until
after finalization) can enlarge the correct BH/DSR universe. Closing that gap
requires predeclared campaign membership plus a registration-closed seal; the
current artifact must be interpreted as a snapshot of the then-visible
conservative universe.

## Test strategy

Development follows red-green-refactor. The first red batch demonstrates the
five known escape paths: overlapping historical windows, same-close-only alpha,
missing PIT/cutoff, OOS-sensitive ranking, and legacy identity-less eligibility.

Focused tests then cover:

- overlap rejection and a non-overlapping production split;
- causal next-open profit, same-close alpha removal, missing/malformed next bar,
  and final-bar non-fill;
- PIT missing/future/mismatch cases;
- normal, boundary, non-finite, zero-variance, and small-sample DSR/PBO/FDR;
- all three baselines plus cost/MDD/config-hash sensitivity;
- selection invariance to OOS changes and one-time finalize;
- opaque sealed-OOS writer/loader hash binding, JSONB round-trip identity,
  campaign/accounting exclusion, idempotent replay/conflict, and concurrent
  cross-run reuse rejection;
- persisted-score reconstruction, caller-score forgery, missing/legacy,
  non-finite, tied, duplicate, exact-target, strict-number, and all-method
  mismatch evidence;
- all terminal statuses, duplicate/concurrent idempotency, and accounting;
- durable crash recording for subprocess launch, log-write, and revert errors;
- evaluation-window hash sensitivity and cross-campaign isolation in PostgreSQL;
- exact promotion hash linkage, mismatch rejection, and identity-less legacy
  non-promotion;
- ROB-846 canonical hash, append-only, and AST import-guard regression.

The final verification commands are exactly those requested in ROB-847. The
known pre-change Nautilus baseline failures are reported separately: six require
unavailable local OHLCV fixtures and one is an unrelated supported-interval
expectation mismatch on current `origin/main`.

## Documentation and compatibility

`backtest/program.md` will describe the loop as exploratory and non-promotable
unless it supplies registered identity and honest-gate evidence. Statistical
formulae, assumptions, edge cases, reason codes, and one-time OOS semantics are
documented beside the gate implementation.

Existing ROB-320/351/382 validated-gate consumers remain unchanged. The honest
gate is additive and composes existing pure metrics where their semantics match;
it does not retrofit sealed-OOS semantics into legacy APIs.
