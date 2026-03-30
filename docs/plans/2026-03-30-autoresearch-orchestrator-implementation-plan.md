# Autoresearch Orchestrator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a bounded multi-round backtest orchestrator with manual and AI-driven modes, plus the supporting documentation and focused tests.

**Architecture:** The orchestrator will be a standalone CLI in `backtest/orchestrator.py` that wraps the existing `backtest/run_experiment.py` loop without duplicating scoring or revert logic. It will maintain only control-plane state, poll for new commits in `manual` mode, invoke an external AI CLI in `auto` mode, and derive progress from `results.tsv` plus git state. Tests will cover deterministic helpers and loop decisions rather than subprocess-heavy end-to-end behavior.

**Tech Stack:** Python 3.13, standard library (`argparse`, `subprocess`, `signal`, `shutil`, `time`, `pathlib`), pytest

---

### Task 1: Add failing tests for orchestrator helper behavior

**Files:**
- Create: `tests/backtest/test_orchestrator.py`
- Read: `backtest/run_experiment.py`
- Read: `tests/backtest/conftest.py`

**Step 1: Write the failing test**

Add tests for:

```python
def test_get_best_cv_score_reads_only_keep_rows(tmp_path): ...
def test_read_last_result_row_returns_latest_entry(tmp_path): ...
def test_resolve_description_prefers_cli_value(tmp_path): ...
def test_update_stats_treats_auto_no_commit_as_skip() -> None: ...
def test_format_duration_renders_human_readable_output() -> None: ...
```

These tests should target pure helper functions that will live in `backtest/orchestrator.py`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_orchestrator.py -q`
Expected: FAIL because `backtest/orchestrator.py` and its helper functions do not exist yet.

**Step 3: Commit**

Do not commit yet. Continue directly to the minimal implementation so the new tests can turn green in the next task.

### Task 2: Implement orchestrator helper layer and CLI skeleton

**Files:**
- Create: `backtest/orchestrator.py`
- Read: `backtest/program.md`
- Read: `results.tsv`

**Step 1: Write minimal implementation**

Create `backtest/orchestrator.py` with:

- CLI parsing for `--mode`, `--rounds`, `--timeout`, `--max-consecutive-reverts`, `--ai-cli`, `--description`, `--poll-interval`, `--ai-timeout`
- `Path` constants for repo root and `results.tsv`
- helper functions for:
  - reading best kept score from `results.tsv`
  - reading the last result row
  - resolving the description from CLI override or git commit subject
  - formatting durations
  - updating counters for `keep`, `revert`, `crash`, and `skip`
- a `Stats` dataclass for cumulative state

Keep the first implementation minimal but structured for later loop wiring.

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_orchestrator.py -q`
Expected: PASS for the pure helper coverage from Task 1.

### Task 3: Add the full round loop, safety checks, and manual/auto control flow

**Files:**
- Modify: `backtest/orchestrator.py`
- Read: `backtest/run_experiment.py`

**Step 1: Write the failing test**

Extend `tests/backtest/test_orchestrator.py` with a loop-level test that covers one control-flow rule, for example:

```python
def test_should_stop_on_consecutive_reverts_limit() -> None: ...
def test_manual_mode_waits_for_new_head(monkeypatch) -> None: ...
```

Keep subprocess interactions mocked. The test should fail until the loop-control helper exists.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_orchestrator.py -q`
Expected: FAIL on the newly added loop-control behavior.

**Step 3: Write minimal implementation**

Finish `backtest/orchestrator.py` by adding:

- git helpers for `rev-parse HEAD`, commit-subject lookup, and dirty-tree detection
- free-space check via `shutil.disk_usage`
- graceful `SIGINT` flag handling
- `manual` mode polling for a new commit
- `auto` mode AI CLI invocation plus no-new-commit `skip` behavior
- `run_experiment.py` subprocess execution
- per-round and final summary output
- exit code `3` when consecutive reverts exceed the configured limit

Use `run_experiment.py` as a subprocess only; do not import or rewrite its scoring logic.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_orchestrator.py -q`
Expected: PASS

### Task 4: Update the AI-agent operating guide

**Files:**
- Modify: `backtest/program.md`

**Step 1: Write the failing test**

No automated markdown test is needed. This task is documentation-only.

**Step 2: Write minimal implementation**

Add an `Orchestrator Usage` section covering:

- `manual` and `auto` invocation examples
- the commit-message description fallback
- `manual` mode waiting for new commits
- the `strategy.py`-only edit rule
- safety notes for revert limits, timeout, and machine speed

Adjust the existing experiment-loop guidance so it remains consistent with the orchestrator-driven workflow.

**Step 3: Run lightweight verification**

Run: `rg -n "Orchestrator Usage|manual mode|auto mode|strategy.py" backtest/program.md`
Expected: the new operating instructions are present and visible.

### Task 5: Verify the implementation end to end

**Files:**
- Verify only

**Step 1: Run targeted tests**

Run: `uv run pytest tests/backtest/test_orchestrator.py -q`
Expected: PASS

**Step 2: Run a manual-mode CLI smoke check**

Run: `uv run backtest/orchestrator.py --mode manual --rounds 1 --timeout 5`
Expected: starts, reports current best score, then exits cleanly on timeout or waiting-state boundary without a traceback.

**Step 3: Run lint on the touched files**

Run: `uv run ruff check backtest/orchestrator.py tests/backtest/test_orchestrator.py`
Expected: PASS

**Step 4: Commit**

```bash
git add backtest/orchestrator.py backtest/program.md tests/backtest/test_orchestrator.py docs/plans/2026-03-30-autoresearch-orchestrator-design.md docs/plans/2026-03-30-autoresearch-orchestrator-implementation-plan.md
git commit -m "feat: add autoresearch orchestrator for multi-round experiments"
```
