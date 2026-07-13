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

- [ ] **Step 1: Write the failing overlap regression and valid-default tests**

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

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k evaluation_window`
Expected: FAIL because the admission interfaces do not exist.

- [ ] **Step 3: Implement deterministic closed-interval validation and move fold 4 before sealed OOS**

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

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k 'evaluation_window or cross_validate'`
Expected: all selected tests pass.

- [ ] **Step 5: Commit**

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

- [ ] **Step 1: Write failing causal fixtures**

Add synthetic tests proving:

```python
assert same_close_alpha_result.total_return_pct <= 0
assert causal_next_open_result.total_return_pct > 0
assert result.trade_log[0]["signal_date"] < result.trade_log[0]["date"]
assert no_next_bar_result.num_trades == 0
assert malformed_next_open_result.num_trades == 0
assert final_bar_signal_result.num_trades == 0
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_prepare.py -k 'same_close or next_open or next_bar or final_bar'`
Expected: same-close fixture profits or new audit fields/interfaces are missing.

- [ ] **Step 3: Implement pending-signal carry and frozen fill costs**

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

- [ ] **Step 4: Run GREEN and the full backtest suite**

Run: `uv run pytest -q tests/backtest/test_prepare.py`
Expected: pass.

Run: `uv run pytest -q tests/backtest`
Expected: pass.

- [ ] **Step 5: Commit**

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

- [ ] **Step 1: Write failing tests for statistics, PIT, baselines, hashes, and OOS isolation**

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

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q research/nautilus_scalping/tests/test_honest_offline_gate.py research/nautilus_scalping/tests/test_frozen_config.py`
Expected: import/interface failures.

- [ ] **Step 3: Implement fail-closed pure functions**

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
cost stress, MDD, PIT, hashes, and canonical artifact hash.

- [ ] **Step 4: Run GREEN and Nautilus pure-gate regressions**

Run: `uv run pytest -q research/nautilus_scalping/tests/test_honest_offline_gate.py research/nautilus_scalping/tests/test_frozen_config.py research/nautilus_scalping/tests/test_validated_gate.py research/nautilus_scalping/tests/test_gate_stats_hardening.py`
Expected: pass.

- [ ] **Step 5: Commit**

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

- [ ] **Step 1: Write failing integration/unit tests**

Cover accounting-derived trial count, all outcome counts, exact hash linkage,
hash mismatch, identity-less run, missing PIT, one-time finalize, and concurrent
finalize. Assert caller input has no trial-count parameter.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/services/research/test_research_offline_gate_service.py`
Expected: service import failure.

- [ ] **Step 3: Implement the async adapter without direct model writes for promotion**

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

- [ ] **Step 4: Run GREEN plus ROB-846 regressions and AST guard**

Run: `uv run pytest -q tests/services/research`
Expected: pass or DB-dependent tests skip under the existing fixture policy.

- [ ] **Step 5: Commit**

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

- [ ] **Step 1: Write failing tests for every status and record-before-revert ordering**

Parameterize `completed`, `rejected`, `crashed`, and `timeout`. Use call-order
spies to assert `record_trial` completes before `git_revert`. Reinvoke with the
same key and assert the registry returns the original row.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/backtest/test_run_experiment.py`
Expected: registered lifecycle interfaces missing.

- [ ] **Step 3: Implement registered mode and explicit legacy non-promotion**

Parse a canonical identity JSON path plus `information_cutoff` and
`idempotency_key`. Map successful parsed evaluation to `completed`, policy/no
improvement to `rejected`, timeout to `timeout`, and process/parse failures to
`crashed`. Commit the registry transaction before invoking git revert. Without
identity input, retain exploratory TSV behavior but emit
`missing_experiment_identity` and never invoke promotion.

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/backtest/test_run_experiment.py tests/backtest/test_orchestrator.py`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add backtest/run_experiment.py tests/backtest/test_run_experiment.py backtest/program.md
git commit -m "feat(ROB-847): record every registered research trial"
```

### Task 6: Full regression and completion evidence

**Files:**
- Modify only files required to fix failures caused by Tasks 1-5.

**Interfaces:**
- Verifies all ROB-847 contracts and preserves known unrelated baseline failures.

- [ ] **Step 1: Run requested test groups**

```bash
uv run pytest -q tests/backtest
uv run pytest -q research/nautilus_scalping/tests
uv run pytest -q tests/services/research tests/test_research_ingestion_service.py
```

- [ ] **Step 2: Repeat related tests with xdist**

```bash
uv run pytest -q -n auto --dist loadfile tests/backtest/test_prepare.py tests/backtest/test_run_experiment.py research/nautilus_scalping/tests/test_honest_offline_gate.py tests/services/research/test_research_offline_gate_service.py
```

- [ ] **Step 3: Run static and repository checks**

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
git diff --check
uv run pytest -q tests/services/research/test_no_broker_import_guard.py
```

- [ ] **Step 4: Audit scope and requirements**

Confirm no migration, Binance Demo execution/ledger, broker/order/fill import,
or unrelated strategy-search file appears in `git diff origin/main...HEAD`.
Record exact pass/fail/skip counts and distinguish the seven known Nautilus
baseline failures from ROB-847 regressions.

- [ ] **Step 5: Commit any verification-only corrections separately**

If verification requires a correction, stage each named file shown by
`git status --short`, inspect `git diff --cached`, and commit only that correction
with `git commit -m "fix(ROB-847): close honest gate verification gaps"`. If no
correction is required, do not create an empty commit.

- [ ] **Step 6: Comment on Linear and retain In Progress**

Post root cause, formulas, red-to-green evidence, per-command counts, commit
SHAs, known baseline limitations, and no-migration/no-execution-ledger scope.
Verify ROB-847 remains `In Progress`; do not push, open a PR, or merge.
