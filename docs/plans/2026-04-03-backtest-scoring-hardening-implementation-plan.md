# Backtest Scoring Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden `compute_score()` so low-exposure, low-return, score-hacking strategies stop ranking as strong backtests while keeping the existing CV aggregation formula unchanged.

**Architecture:** Extend `BacktestResult` with an engine-owned `time_in_market_pct` field, compute it once inside `run_backtest()`, and make all consumers read the official value instead of recomputing it ad hoc. Keep the scoring change localized to `backtest/prepare.py`, preserve `cross_validate()` as-is, and add targeted regression tests that model the exploit pattern directly.

**Tech Stack:** Python 3.13, dataclasses, pandas, numpy, pytest, uv

---

### Task 1: Lock The New Scoring Contract In Tests

**Files:**
- Modify: `tests/backtest/test_prepare.py`

**Step 1: Write the failing contract tests**

Add or replace focused tests that describe the approved scoring behavior:

```python
def test_score_formula_applies_progressive_penalties() -> None:
    result = prepare.BacktestResult(
        total_return_pct=1.0,
        sharpe=1.5,
        max_drawdown_pct=25.0,
        num_trades=20,
        win_rate_pct=0.5,
        profit_factor=1.5,
        avg_holding_days=1.0,
        time_in_market_pct=10.0,
        equity_curve=[100000.0, 101000.0],
    )

    score = prepare.compute_score(result)
    expected = (
        1.5
        - (25.0 - 20.0) * 0.1
        - (15 - (20 // 2)) * 0.2
        - (20.0 - 10.0) * 0.1
        - (2.0 - 1.0) * 0.5
        - 0.5
    )
    assert score == pytest.approx(expected, abs=0.01)


def test_score_hacking_profile_scores_at_or_below_two() -> None:
    result = prepare.BacktestResult(
        total_return_pct=0.8,
        sharpe=4.28,
        max_drawdown_pct=0.03,
        num_trades=88,
        win_rate_pct=0.955,
        profit_factor=828.0,
        avg_holding_days=1.0,
        time_in_market_pct=10.7,
        equity_curve=[100000.0, 100800.0],
    )
    assert prepare.compute_score(result) <= 2.0
```

Also add a positive-path test showing a compliant strategy keeps its Sharpe-based score with no new penalties.

**Step 2: Add engine-level `time_in_market_pct` expectations**

Add a `run_backtest()` test that buys on the first bar, holds across one middle bar, and exits on the last bar so the expected exposure is deterministic:

```python
assert result.time_in_market_pct == pytest.approx(66.67, abs=0.1)
```

Use the existing three-bar fixture style already present in `TestRunBacktest`.

**Step 3: Run tests to verify failure**

Run: `uv run pytest tests/backtest/test_prepare.py -k "score_formula or score_hacking or time_in_market" -v`

Expected: FAIL because `BacktestResult` does not yet expose `time_in_market_pct` and `compute_score()` still uses the old two-penalty formula.

**Step 4: Commit**

```bash
git add tests/backtest/test_prepare.py
git commit -m "test: lock backtest scoring hardening contract"
```

### Task 2: Extend `BacktestResult` And Compute Official Time-In-Market

**Files:**
- Modify: `backtest/prepare.py`
- Modify: `tests/backtest/test_prepare.py`

**Step 1: Add the result field**

Extend `BacktestResult` with a defaulted field so existing fixture construction keeps working:

```python
@dataclass
class BacktestResult:
    ...
    avg_holding_days: float
    time_in_market_pct: float = 0.0
    backtest_seconds: float = 0.0
```

Do not reorder the required fields above `avg_holding_days`.

**Step 2: Track exposure inside `run_backtest()`**

Count bars where at least one position remains open after the day’s signals execute:

```python
days_in_market = 0

for date in dates:
    ...
    for signal in signals:
        state = _execute_signal(signal, state, bar_data, portfolio_value)

    if state.positions:
        days_in_market += 1
```

This preserves the same semantics already used in `backtest/report.py`: a bar counts only if the portfolio is still exposed after that day’s executions.

**Step 3: Thread the metric into `_build_result()`**

Pass the derived percentage into `_build_result()` and the empty-result branch:

```python
time_in_market_pct = days_in_market / len(dates) * 100.0

return _build_result(
    state,
    equity_curve,
    elapsed,
    bar_interval=bar_interval,
    equity_dates=equity_dates,
    time_in_market_pct=time_in_market_pct,
)
```

Update `_build_result()` to accept the new argument and store it on `BacktestResult`.

**Step 4: Run tests to verify pass**

Run: `uv run pytest tests/backtest/test_prepare.py -k "time_in_market" -v`

Expected: PASS with the new exposure metric populated by the engine.

**Step 5: Commit**

```bash
git add backtest/prepare.py tests/backtest/test_prepare.py
git commit -m "feat: add official time-in-market metric to backtest results"
```

### Task 3: Harden `compute_score()` Against Score Hacking

**Files:**
- Modify: `backtest/prepare.py`
- Modify: `tests/backtest/test_prepare.py`

**Step 1: Replace the old formula with the approved composite score**

Update `compute_score()` to keep Sharpe as the base and apply these penalties:

```python
def compute_score(result: BacktestResult) -> float:
    score = result.sharpe

    if result.max_drawdown_pct > 20:
        score -= (result.max_drawdown_pct - 20) * 0.1

    round_trips = result.num_trades // 2
    if round_trips < 15:
        score -= (15 - round_trips) * 0.2

    if result.time_in_market_pct < 20.0:
        score -= (20.0 - result.time_in_market_pct) * 0.1

    if result.total_return_pct < 2.0:
        score -= (2.0 - result.total_return_pct) * 0.5

    if result.avg_holding_days < 1.5:
        score -= 0.5

    return score
```

Skip the BTC buy-and-hold benchmark in this change. It was explicitly marked optional and would broaden scope into benchmark data plumbing.

**Step 2: Update the docstring and comments**

Document the anti-gaming intent directly above the function so future changes do not silently revert to Sharpe-only optimization.

**Step 3: Run the scoring-focused tests**

Run: `uv run pytest tests/backtest/test_prepare.py -k "score_formula or score_hacking or few_trades" -v`

Expected: PASS with the new progressive penalty expectations.

**Step 4: Commit**

```bash
git add backtest/prepare.py tests/backtest/test_prepare.py
git commit -m "fix: harden compute_score against score hacking"
```

### Task 4: Make Report And CLI Consumers Use The Official Exposure Field

**Files:**
- Modify: `backtest/report.py`
- Modify: `backtest/backtest.py`
- Modify: `tests/backtest/test_report.py`

**Step 1: Add report coverage for the new field**

Add a payload-level test that proves the report uses `BacktestResult.time_in_market_pct` instead of relying only on a local recomputation:

```python
def test_build_report_payload_prefers_result_time_in_market_pct() -> None:
    result = _make_result()
    result.time_in_market_pct = 37.5

    payload = report.build_report_payload(...)

    assert payload["risk_metrics"]["time_in_market_pct"] == pytest.approx(37.5)
```

**Step 2: Update report generation**

Keep `_time_in_market_pct()` as a fallback helper for raw trade-log analysis, but thread the official field through the payload path:

```python
risk_metrics = generate_risk_metrics(...)
risk_metrics["time_in_market_pct"] = result.time_in_market_pct
summary["time_in_market_pct"] = result.time_in_market_pct
```

If you prefer a cleaner interface, change `generate_risk_metrics()` to accept an optional override:

```python
def generate_risk_metrics(..., time_in_market_pct: float | None = None) -> dict[str, Any]:
    ...
```

**Step 3: Surface the metric in single-run CLI output**

Extend `_print_result()` so manual verification of score-hacking cases shows the new metric directly:

```python
print(f"time_in_market_pct:{result.time_in_market_pct:10.1f}%")
```

Do not change `orchestrator.py` or `backtest/run_experiment.py`.

**Step 4: Run report-focused tests**

Run: `uv run pytest tests/backtest/test_report.py -v`

Expected: PASS with the payload reflecting the official exposure metric.

**Step 5: Commit**

```bash
git add backtest/report.py backtest/backtest.py tests/backtest/test_report.py
git commit -m "feat: surface official backtest exposure metric in reports"
```

### Task 5: Run Regression And Behavior Verification

**Files:**
- Modify only if verification exposes a real defect

**Step 1: Run the targeted pytest suite**

Run: `uv run pytest tests/backtest/test_prepare.py tests/backtest/test_report.py -v`

Expected: PASS

**Step 2: Verify single-run output**

Run: `uv run backtest/backtest.py --mode single`

Expected: output now includes `time_in_market_pct` alongside the other summary metrics, and score calculation completes without error.

**Step 3: Verify report mode**

Run: `uv run backtest/backtest.py --mode report`

Expected: report output completes successfully and risk metrics show the same exposure percentage the engine computed.

**Step 4: Verify CV mode remains unchanged structurally**

Run: `uv run backtest/backtest.py --mode cv`

Expected: existing `cv_score = mean - 0.5 * std - catastrophic penalty` flow remains intact, with only fold-level scores shifting because `compute_score()` changed.

**Step 5: Sanity-check the anti-hacking target**

Re-run the known exploit configuration or reproduce it with a fixture/result object and confirm the resulting score is `<= 2.0`.

**Step 6: Commit**

```bash
git add backtest/prepare.py backtest/report.py backtest/backtest.py tests/backtest/test_prepare.py tests/backtest/test_report.py
git commit -m "test: verify hardened backtest scoring workflow"
```
