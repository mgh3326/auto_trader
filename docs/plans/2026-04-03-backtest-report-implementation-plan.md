# Backtest Report Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a report mode that prints and exports detailed split and cross-validation backtest analysis without changing strategy behavior.

**Architecture:** Extend `BacktestResult` with equity timestamps, keep the engine responsible only for producing raw results, and move all report aggregation and formatting into `backtest/report.py`. Update the CLI to orchestrate single-run plus CV execution and select text or JSON rendering.

**Tech Stack:** Python 3.13, argparse, dataclasses, pandas, pytest, json

---

### Task 1: Lock the Result Contract

**Files:**
- Modify: `tests/backtest/test_prepare.py`
- Modify: `backtest/prepare.py`

**Step 1: Write the failing test**

Add assertions that `BacktestResult` accepts `equity_dates` and `run_backtest()` returns aligned `equity_dates` and `equity_curve`.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_prepare.py -k "equity_dates" -v`
Expected: FAIL because `BacktestResult` has no `equity_dates` field yet.

**Step 3: Write minimal implementation**

Add `equity_dates` to `BacktestResult`. Populate it in `run_backtest()` and `_build_result()`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_prepare.py -k "equity_dates" -v`
Expected: PASS

### Task 2: Add Report Aggregation Tests

**Files:**
- Create: `tests/backtest/test_report.py`
- Create: `backtest/report.py`

**Step 1: Write the failing test**

Add focused tests for:

- round-trip reconstruction with partial sells
- monthly returns table rows
- risk metrics keys
- top-level payload shape

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_report.py -v`
Expected: FAIL because `backtest/report.py` does not exist yet.

**Step 3: Write minimal implementation**

Implement only the helpers needed to satisfy the tests:

- round-trip builder
- monthly row generator
- risk metric generator
- payload generator

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_report.py -v`
Expected: PASS

### Task 3: Add CLI Report Mode

**Files:**
- Modify: `backtest/backtest.py`
- Modify: `tests/backtest/test_strategy_regression.py` if import contract assertions need expansion

**Step 1: Write the failing test**

Add CLI-focused test coverage if needed for new parser choices or import contract stability.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/backtest/test_strategy_regression.py -k "backtest_runner" -v`
Expected: FAIL only if the new CLI changes are not yet reflected in tests.

**Step 3: Write minimal implementation**

Add:

- `report` to `--mode`
- `--output text|json`
- `_run_report()`
- report rendering call path

Keep `single` and `cv` flows unchanged.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/backtest/test_strategy_regression.py -k "backtest_runner" -v`
Expected: PASS

### Task 4: Run Targeted Regression Verification

**Files:**
- Modify only if failures reveal real regressions

**Step 1: Run targeted tests**

Run: `uv run pytest tests/backtest/test_prepare.py tests/backtest/test_report.py tests/backtest/test_strategy_regression.py -v`

**Step 2: Fix failures minimally**

Keep changes within `backtest/prepare.py`, `backtest/backtest.py`, `backtest/report.py`, and the test files.

**Step 3: Re-run targeted tests**

Run the same pytest command until green.

### Task 5: Run CLI Verification

**Files:**
- No code changes unless verification reveals an actual defect

**Step 1: Verify text report**

Run: `uv run backtest/backtest.py --mode report`
Expected: text report with summary, monthly, per-symbol, top/bottom trades, CV, risk metrics

**Step 2: Verify JSON report**

Run: `uv run backtest/backtest.py --mode report --output json`
Expected: valid JSON with the approved top-level keys

**Step 3: Verify legacy modes**

Run:

- `uv run backtest/backtest.py --mode single`
- `uv run backtest/backtest.py --mode cv`

Expected: both commands still succeed and keep legacy output semantics
