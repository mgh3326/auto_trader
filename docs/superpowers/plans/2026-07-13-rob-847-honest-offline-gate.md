# ROB-847 Honest Offline Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a causal, PIT-bound, trial-aware, sealed-OOS promotion gate that reuses the ROB-846 immutable registry and creates no migration.

**Architecture:** Keep causal fill and split admission in `backtest/prepare.py`; add a pure honest-gate statistics/sealing module beside the existing Nautilus research gate; add one async application service that reads ROB-846 accounting and creates the exact promotion link. The legacy experiment runner records registered terminal outcomes but remains non-authoritative for promotion.

**Tech Stack:** Python 3.13, pandas/numpy, stdlib `statistics.NormalDist`, Pydantic, SQLAlchemy async, pytest/pytest-asyncio/xdist, Ruff, ty.

## Global Constraints

- Reuse ROB-846 schemas, registry functions, typed canonical AST, immutable hashes, and append-only trial accounting.
- Do not create a migration unless implementation proves the existing unique promotion link cannot seal finalize; stop and report before expanding scope.
- Do not touch Binance Demo execution or ledger files.
- Do not import broker, order, or fill modules into the research registry/gate boundary.
- Do not use sealed OOS values in parameter selection or ranking.
- Record registered terminal outcomes before any legacy git revert.
- Keep legacy identity-less results non-promotable.

---

### Task 1: Evaluation-window admission

**Files:**
- Modify: `backtest/prepare.py`
- Modify: `tests/backtest/test_prepare.py`

**Interfaces:**
- Produces: `validate_evaluation_windows(windows: dict[str, dict[str, str]]) -> None`
- Produces: `EvaluationWindowError` carrying `reason_code` and overlapping window names.

- [x] **Step 1: Write the failing overlap regression and valid-default tests**

```python
def test_historical_fold_four_overlaps_sealed_test() -> None:
    windows = prepare.evaluation_windows(
        splits={"test": {"start": "2026-02-01", "end": "2026-03-22"}},
        folds=[{"val_start": "2026-01-01", "val_end": "2026-03-22"}],
    )
    with pytest.raises(prepare.EvaluationWindowError) as exc:
        prepare.validate_evaluation_windows(windows)
    assert exc.value.reason_code == "overlapping_evaluation_windows"
    assert exc.value.window_names == ("cv_fold_1_validation", "sealed_oos")

def test_default_evaluation_windows_do_not_overlap() -> None:
    prepare.validate_evaluation_windows(prepare.default_evaluation_windows())
```

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k evaluation_window`
Expected: FAIL because the admission interfaces do not exist.

- [x] **Step 3: Implement deterministic closed-interval validation and move fold 4 before sealed OOS**

```python
class EvaluationWindowError(ValueError):
    reason_code = "overlapping_evaluation_windows"

    def __init__(self, left: str, right: str) -> None:
        self.window_names = tuple(sorted((left, right)))
        super().__init__(f"{self.reason_code}: {self.window_names[0]}, {self.window_names[1]}")
```

Build named validation windows from `CV_FOLDS`, name `SPLITS["test"]` as
`sealed_oos`, reject invalid bounds, and compare every pair using
`left.start <= right.end and right.start <= left.end`. Set production fold 4
validation end to `2026-01-31`.

- [x] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k 'evaluation_window or cross_validate'`
Expected: all selected tests pass.

- [x] **Step 5: Commit**

```bash
git add backtest/prepare.py tests/backtest/test_prepare.py
git commit -m "fix(ROB-847): reject overlapping evaluation windows"
```

### Task 2: Causal next-open execution

**Files:**
- Modify: `backtest/prepare.py`
- Modify: `tests/backtest/test_prepare.py`

**Interfaces:**
- Produces: `ExecutionCost` with frozen fee/spread/slippage.
- Changes: `_execute_signal` accepts a validated executable price and separate signal/fill timestamps.
- Changes: `run_backtest` carries pending signals exactly one chronological bar.

- [x] **Step 1: Write failing causal fixtures**

Add synthetic tests proving:

```python
assert same_close_alpha_result.total_return_pct <= 0
assert causal_next_open_result.total_return_pct > 0
assert result.trade_log[0]["signal_date"] < result.trade_log[0]["date"]
assert no_next_bar_result.num_trades == 0
assert malformed_next_open_result.num_trades == 0
assert final_bar_signal_result.num_trades == 0
```

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k 'same_close or next_open or next_bar or final_bar'`
Expected: same-close fixture profits or new audit fields/interfaces are missing.

- [x] **Step 3: Implement pending-signal carry and frozen fill costs**

```python
@dataclass(frozen=True)
class ExecutionCost:
    fee_rate: float = TRADING_FEE
    half_spread_bps: float = 0.0
    slippage_bps: float = SLIPPAGE_BPS

def _validated_open(bar: BarData) -> float | None:
    value = float(bar.open)
    return value if np.isfinite(value) and value > 0 else None
```

At each date: build bars; execute only prior pending signals at current valid
opens; mark equity at current closes; call `strategy.on_bar`; replace pending
signals with the returned list. Never drain pending signals after the loop.

- [x] **Step 4: Run GREEN and the full backtest suite**

Run: `uv run pytest -q tests/backtest/test_prepare.py`
Expected: pass.

Run: `uv run pytest -q tests/backtest`
Expected: pass.

- [x] **Step 5: Commit**

```bash
git add backtest/prepare.py tests/backtest/test_prepare.py
git commit -m "fix(ROB-847): execute signals at causal next opens"
```

### Task 3: Pure statistical and frozen-config gate

**Files:**
- Create: `research/nautilus_scalping/honest_offline_gate.py`
- Create: `research/nautilus_scalping/tests/test_honest_offline_gate.py`
- Modify: `research/nautilus_scalping/frozen_config.py`
- Modify: `research/nautilus_scalping/tests/test_frozen_config.py`

**Interfaces:**
- Produces: `HonestGateConfig`, `PITEvidence`, `SelectionCandidate`, `SelectionResult`, `SealedOOS`, and `GateArtifact`.
- Produces: `select_parameters`, `validate_pit_evidence`, `deflated_sharpe_ratio`, `probability_backtest_overfitting`, `benjamini_hochberg`, and `build_gate_artifact`.

- [x] **Step 1: Write failing tests for statistics, PIT, baselines, hashes, and OOS isolation**

Tests cover finite normal cases plus small-sample, zero variance, non-finite,
boundary equality, PIT missing/future/mismatch, and deterministic reason codes.
Pin selection isolation with:

```python
first = select_parameters(candidates)
second = select_parameters(candidates)
assert first == second
with pytest.raises(TypeError):
    select_parameters(candidates, sealed_oos=changed_oos)
```

Assert changing each threshold, baseline, cost, or MDD definition changes
`config_hash()`.

- [x] **Step 2: Run RED**

Run: `uv run pytest -q research/nautilus_scalping/tests/test_honest_offline_gate.py research/nautilus_scalping/tests/test_frozen_config.py`
Expected: import/interface failures.

- [x] **Step 3: Implement fail-closed pure functions**

Use `statistics.NormalDist` for `cdf`/`inv_cdf`; calculate DSR with the approved
Bailey–López de Prado formula, CSCV PBO over all half-slice combinations, and
Benjamini-Hochberg over finite p-values. Return stable reason codes for invalid
inputs instead of pass-like numeric defaults. Sort/de-duplicate reasons before
artifact hashing.

Three baseline inputs are required by exact key:

```python
REQUIRED_BASELINES = ("cash", "btc_eth_equal_weight", "same_turnover_random")
```

The artifact includes accounting, DSR/PBO/FDR, fold/OOS metrics, baselines,
cost stress, PIT, hashes, and canonical artifact hash. Observed MDD comes only
from the required finite, non-negative `max_drawdown_pct` in the hash-bound
sealed-OOS artifact; finalize has no caller MDD input.

- [x] **Step 4: Run GREEN and Nautilus pure-gate regressions**

Run: `uv run pytest -q research/nautilus_scalping/tests/test_honest_offline_gate.py research/nautilus_scalping/tests/test_frozen_config.py research/nautilus_scalping/tests/test_validated_gate.py research/nautilus_scalping/tests/test_gate_stats_hardening.py`
Expected: pass.

- [x] **Step 5: Commit**

```bash
git add research/nautilus_scalping/honest_offline_gate.py research/nautilus_scalping/frozen_config.py research/nautilus_scalping/tests/test_honest_offline_gate.py research/nautilus_scalping/tests/test_frozen_config.py
git commit -m "feat(ROB-847): add trial-aware honest gate statistics"
```

### Task 4: Registry-backed one-time finalize and promotion linkage

**Files:**
- Create: `app/services/research_offline_gate_service.py`
- Create: `tests/services/research/test_research_offline_gate_service.py`
- Modify: `tests/services/research/test_no_broker_import_guard.py`

**Interfaces:**
- Produces: `finalize_offline_gate(session, *, backtest_run_id, experiment_id, expected_config_hash, expected_data_hash, selection, sealed_oos, pit_evidence, statistics_evidence) -> ResearchPromotionCandidate`.
- Consumes: `get_trial_accounting` and `link_promotion_candidate` from ROB-846.

- [x] **Step 1: Write failing integration/unit tests**

Cover accounting-derived trial count, all outcome counts, exact hash linkage,
hash mismatch, identity-less run, missing PIT, one-time finalize, and concurrent
finalize. Assert caller input has no trial-count parameter.

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/services/research/test_research_offline_gate_service.py`
Expected: service import failure.

- [x] **Step 3: Implement the async adapter without direct model writes for promotion**

```python
accounting = await get_trial_accounting(session, experiment_id)
artifact = build_gate_artifact(
    accounting=accounting.model_dump(),
    selection=selection,
    sealed_oos=sealed_oos,
    pit_evidence=pit_evidence,
    statistics_evidence=statistics_evidence,
)
request = PromotionLinkRequest(
    expected_experiment_id=experiment_id,
    expected_config_hash=expected_config_hash,
    expected_data_hash=expected_data_hash,
    status="eligible" if artifact.promotable else "non_promotable",
    reason_code=artifact.primary_reason,
    thresholds=artifact.thresholds,
    metrics=artifact.to_metrics(),
)
return await link_promotion_candidate(session, backtest_run_id=backtest_run_id, request=request)
```

Check for an existing candidate before OOS evaluation and normalize the unique
constraint race as `sealed_oos_already_finalized`.

- [x] **Step 4: Run GREEN plus ROB-846 regressions and AST guard**

Run: `uv run pytest -q tests/services/research`
Expected: pass or DB-dependent tests skip under the existing fixture policy.

- [x] **Step 5: Commit**

```bash
git add app/services/research_offline_gate_service.py tests/services/research/test_research_offline_gate_service.py tests/services/research/test_no_broker_import_guard.py
git commit -m "feat(ROB-847): seal OOS promotion through ROB-846 registry"
```

### Task 5: Complete trial recording in the legacy experiment runner

**Files:**
- Modify: `backtest/run_experiment.py`
- Create: `tests/backtest/test_run_experiment.py`
- Modify: `backtest/program.md`

**Interfaces:**
- Produces: async `record_terminal_trial` adapter using `register_experiment` and `record_trial`.
- Adds: registered identity input and stable invocation idempotency key.

- [x] **Step 1: Write failing tests for every status and record-before-revert ordering**

Parameterize `completed`, `rejected`, `crashed`, and `timeout`. Use call-order
spies to assert `record_trial` is attempted before `git_revert`. Reinvoke with
the same key and assert the registry returns the original row. The final
implementation must still attempt revert and the TSV audit if durable recording
fails, preserving all finalization errors.

- [x] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_run_experiment.py`
Expected: registered lifecycle interfaces missing.

- [x] **Step 3: Implement registered mode and explicit legacy non-promotion**

Parse a canonical identity JSON path plus `information_cutoff` and
`idempotency_key`. Map successful parsed evaluation to `completed`, policy/no
improvement to `rejected`, timeout to `timeout`, and process/parse failures to
`crashed`. Commit the registry transaction before invoking git revert. Without
identity input, retain exploratory TSV behavior but emit
`missing_experiment_identity` and never invoke promotion.

- [x] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/backtest/test_run_experiment.py tests/backtest/test_orchestrator.py`
Expected: pass.

- [x] **Step 5: Commit**

```bash
git add backtest/run_experiment.py tests/backtest/test_run_experiment.py backtest/program.md
git commit -m "feat(ROB-847): record every registered research trial"
```

### Task 6: Full regression and completion evidence

**Files:**
- Modify only files required to fix failures caused by Tasks 1-5.

**Interfaces:**
- Verifies all ROB-847 contracts and preserves known unrelated baseline failures.

- [x] **Step 1: Run requested test groups**

```bash
uv run pytest -q tests/backtest
uv run pytest -q research/nautilus_scalping/tests
uv run pytest -q tests/services/research tests/test_research_ingestion_service.py
```

- [x] **Step 2: Repeat related tests with xdist**

```bash
uv run pytest -q -n auto --dist loadfile tests/backtest/test_prepare.py tests/backtest/test_run_experiment.py research/nautilus_scalping/tests/test_honest_offline_gate.py tests/services/research/test_research_offline_gate_service.py
```

- [x] **Step 3: Run static and repository checks**

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
git diff --check
uv run pytest -q tests/services/research/test_no_broker_import_guard.py
```

- [x] **Step 4: Audit scope and requirements**

Confirm no migration, Binance Demo execution/ledger, broker/order/fill import,
or unrelated strategy-search file appears in `git diff origin/main...HEAD`.
Record exact pass/fail/skip counts and distinguish the seven known Nautilus
baseline failures from ROB-847 regressions.

- [x] **Step 5: Commit any verification-only corrections separately**

If verification requires a correction, stage each named file shown by
`git status --short`, inspect `git diff --cached`, and commit only that correction
with `git commit -m "fix(ROB-847): close honest gate verification gaps"`. If no
correction is required, do not create an empty commit.

- [x] **Step 6: Comment on Linear and retain In Progress**

Post root cause, formulas, red-to-green evidence, per-command counts, commit
SHAs, known baseline limitations, and no-migration/no-execution-ledger scope.
Verify ROB-847 remains `In Progress`; do not push, open a PR, or merge.

## Independent review follow-up (P1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development` for every blocker. This follow-up is
> executed in the existing isolated ROB-847 worktree. Do not commit, push, open
> a PR, merge, or update Linear in this session.

### Task 7: Experiment candidate identity and exact target trial

**Files:**
- Modify: `app/services/research_offline_gate_service.py`
- Modify: `research_contracts/honest_offline_gate.py`
- Test: `tests/services/research/test_research_offline_gate_service.py`

**Interfaces:**
- Candidate keys are immutable `ResearchStrategyExperiment.experiment_id`
  values, not `params_hash` values.
- A server candidate retains `params_hash` provenance and the exact evaluated
  `ResearchBacktestRun.id` that supplied evidence.
- The target run must itself be the completed/rejected evidence row and the
  server-selected experiment candidate.

- [x] Add unit and real-PostgreSQL red tests for distinct experiments sharing
  params, crashed target plus completed sibling, and missing exact target
  evidence.
- [x] Run the focused tests and verify they fail because params aliases and
  sibling evidence are currently accepted.
- [x] Key the campaign/evidence map by `experiment_id`, preserve a
  candidate-to-params provenance map in the artifact, and add exact target-run
  checks.
- [x] Run the focused unit/PG/concurrency tests green.

### Task 8: Canonical evaluation-window identity

**Files:**
- Create: `research_contracts/evaluation_windows.py`
- Modify: `research_contracts/frozen_config.py`
- Modify: `backtest/prepare.py`
- Modify: `tests/backtest/test_prepare.py`
- Modify: `research/nautilus_scalping/tests/test_frozen_config.py`
- Modify: `tests/services/research/test_research_offline_gate_service.py`
- Modify: `tests/services/research/test_research_contracts_wheel.py`

**Interfaces:**
- `CANONICAL_EVALUATION_WINDOWS` is the single immutable source for train,
  validation, sealed-OOS, and CV fold closed intervals.
- `CampaignConfig.evaluation_windows` serializes into both `config_hash()` and
  `policy_identity()`.
- `prepare.SPLITS` and `prepare.CV_FOLDS` are derived consumers only.

- [x] Add red tests proving prepare consumes one authority, any window change
  changes config/policy hashes, and different window identities are not pooled.
- [x] Implement the neutral immutable window contract and config round trip.
- [x] Run prepare/config/campaign and clean-wheel tests green.

### Task 9: Complete trial-method provenance and strict JSON numbers

**Files:**
- Modify: `research_contracts/trial_evidence.py`
- Modify: `app/services/research_offline_gate_service.py`
- Modify: `tests/services/research/test_honest_trial_evidence.py`
- Modify: `tests/services/research/test_research_offline_gate_service.py`

**Interfaces:**
- `TrialEvidence` preserves `sharpe_method`, `p_value_method`, and
  `selection_score_method`.
- Finalization compares all three values to the frozen config.
- `_finite_number` accepts only finite non-boolean `int`/`float` JSON numbers;
  strings, booleans, `Decimal`, and other coercible objects fail closed.

- [x] Add red parser/builder/finalizer tests for custom method mismatches and
  strict numeric types across costs, Sharpe, p-value, and validation score.
- [x] Implement exact method preservation/comparison and non-coercive parsing.
- [x] Run trial evidence and finalizer suites green, retaining v1 legacy
  `missing_selection_evidence` behavior.

### Task 10: Durable runner crash normalization

**Files:**
- Modify: `backtest/run_experiment.py`
- Modify: `tests/backtest/test_run_experiment.py`

**Interfaces:**
- An `OSError` from subprocess launch or `RUN_LOG.write_text` after
  registration records exactly one committed `crashed` terminal row before
  revert/cleanup.
- Results use status `crashed`; a failed revert propagates only after the
  terminal commit (and result recording) is durable.

- [x] Add unit red tests for record/revert/result ordering and actual-PG red
  tests for launch, log-write, and revert failures.
- [x] Normalize `asyncio.to_thread(run_backtest)` `OSError` paths through one
  crash helper without weakening timeout/process-return handling.
- [x] Run runner unit/PG tests green and assert exactly one terminal row.

### Task 11: Documentation and full verification

**Files:**
- Modify: `docs/superpowers/specs/2026-07-13-rob-847-honest-offline-gate-design.md`
- Modify: `backtest/program.md`
- Modify pinned artifact/config hashes in their owning tests only after the
  canonical payload is final.

- [x] Update candidate identity, exact target row, canonical windows, trial
  methods, strict JSON-number policy, crash normalization, and reason codes.
- [x] Run backtest, research service, Nautilus, PostgreSQL, concurrency,
  xdist, wheel, broker guard, Ruff/format on changed files, ty, and diff checks.
- [x] Confirm no migration or broker/order/fill change and report the two
  residual boundaries: caller-owned PBO returns and unsealed campaign closure.
