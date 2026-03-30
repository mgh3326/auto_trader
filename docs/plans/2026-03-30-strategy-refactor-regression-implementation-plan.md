# Strategy Refactor Regression Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore pre-refactor `backtest/strategy.py` behavior so `uv run backtest/backtest.py --mode cv` returns `cv_score=4.228733 ± 0.001` while keeping the modularized PR #427 structure.

**Architecture:** Treat this as a behavior-parity regression, not a strategy retune. First capture evidence from commit `8177d23`, then lock that behavior into focused regression tests around import semantics, signal evaluation, and buy-weight precedence before touching runtime code. Fix only the deltas that change decisions; keep `PARAMS`, signal registries, and `backtest/indicators.py` extraction unless a parity check proves one of those abstractions changed behavior.

**Tech Stack:** Python 3.13, uv, pytest, pandas, numpy, git

---

### Task 1: Capture Baseline Evidence Before Any Fix

**Files:**
- Modify: none
- Test: none

**Step 1: Save the current refactored strategy and restore the original strategy from `8177d23`**

Run:

```bash
git show 8177d23:backtest/strategy.py > /tmp/strategy_original.py
cp backtest/strategy.py /tmp/strategy_refactored.py
cp /tmp/strategy_original.py backtest/strategy.py
```

Expected: `backtest/strategy.py` now contains the original inline implementation from `8177d23`.

**Step 2: Record the original CV and trade-log outputs**

Run:

```bash
uv run backtest/backtest.py --mode cv | tee /tmp/strategy_original_cv.log
uv run backtest/backtest.py --mode single --split val > /tmp/trades_original.log 2>&1
```

Expected: CV output is approximately:

```text
cv_score: 4.228733
mean_score: 4.619...
std_score: 0.781...
```

**Step 3: Restore the refactored strategy and record the regressed outputs**

Run:

```bash
cp /tmp/strategy_refactored.py backtest/strategy.py
uv run backtest/backtest.py --mode cv | tee /tmp/strategy_refactored_cv.log
uv run backtest/backtest.py --mode single --split val > /tmp/trades_refactored.log 2>&1
diff -u /tmp/trades_original.log /tmp/trades_refactored.log > /tmp/trades_regression.diff || true
```

Expected: CV output remains near the reported regression (`cv_score` around `-0.404077`) and `/tmp/trades_regression.diff` shows the first decision divergence.

**Step 4: Record the first concrete divergence before editing code**

Run:

```bash
sed -n '1,200p' /tmp/trades_regression.diff
```

Expected: one of the suspected behavior changes is visible before the first fold-level score drift, such as a different buy weight, a missing buy, or a premature sell.

**Step 5: Do not commit**

This task is evidence-only. Leave the worktree on the refactored file after evidence capture.

### Task 2: Add Failing Regression Tests For Legacy Strategy Behavior

**Files:**
- Create: `tests/backtest/test_strategy_regression.py`
- Test: `tests/backtest/test_strategy_regression.py`

**Step 1: Add a legacy buy-weight reference helper copied from `8177d23`**

Inside `tests/backtest/test_strategy_regression.py`, add a pure reference helper that preserves the original `if`/`elif` order exactly:

```python
def legacy_buy_weight(symbol: str, bull_flags: dict[str, bool], market_state: dict[str, float]) -> float:
    dual_rsi_oversold = bull_flags["dual_rsi_oversold"]
    macd_histogram_positive = bull_flags["macd_histogram_positive"]
    close_below_bb_lower = bull_flags["close_below_bb_lower"]
    ema_fast_above_slow = bull_flags["ema_fast_above_slow"]
    momentum_positive = bull_flags["momentum_positive"]
    volume_above_avg = bull_flags["volume_above_avg"]

    pure_reversion_buy = (
        dual_rsi_oversold
        and close_below_bb_lower
        and volume_above_avg
        and not macd_histogram_positive
    )
    pure_trend_buy = (
        macd_histogram_positive
        and ema_fast_above_slow
        and momentum_positive
        and volume_above_avg
        and not dual_rsi_oversold
    )
    strong_reversion_buy = (
        dual_rsi_oversold
        and close_below_bb_lower
        and macd_histogram_positive
    )

    if strong_reversion_buy:
        return strategy.STRONG_REVERSION_POSITION_SIZE
    if pure_trend_buy and symbol == "BTC" and market_state["avg_rsi"] >= strategy.BTC_TREND_HOT_RSI_LEVEL and market_state["avg_rsi_change"] < strategy.BTC_TREND_STALL_CHANGE:
        return strategy.BTC_HOT_STALL_TREND_POSITION_SIZE
    ...
    return strategy.POSITION_SIZE
```

Keep the helper intentionally repetitive. This file is the parity oracle, not a refactoring target.

**Step 2: Add table-driven tests that compare current `_resolve_symbol_buy_weight()` against the legacy helper**

Add cases for:

- wildcard `strong_reversion_buy`
- `BTC` hot stall and mid-hot accel
- `SOL` hot stall and low breadth
- `LINK` hot stall
- `XRP` and `ADA` stalled washout
- `DOT` mild reversion
- `ETH` pure reversion
- `AVAX` and `XRP` pure trend
- default `POSITION_SIZE`

Example structure:

```python
@pytest.mark.parametrize(
    ("symbol", "bull_flags", "market_state"),
    [
        ("BTC", {...}, {"avg_rsi": 71.0, "avg_rsi_change": 1.0}),
        ("DOT", {...}, {"avg_rsi": 34.0, "avg_rsi_change": -3.0}),
    ],
)
def test_resolve_symbol_buy_weight_matches_legacy_order(symbol, bull_flags, market_state):
    expected = legacy_buy_weight(symbol, bull_flags, market_state)
    actual, _ = strategy._resolve_symbol_buy_weight(symbol, bull_flags, market_state, strategy.PARAMS)
    assert actual == expected
```

**Step 3: Add signal-parity tests for helper predicates that were extracted during modularization**

Cover:

- `_signal_dual_rsi_oversold()`
- `_setup_pure_reversion_buy()`
- `_setup_strong_reversion_buy()`
- `_setup_pure_trend_buy()`

Example:

```python
def test_signal_dual_rsi_oversold_matches_legacy_none_handling():
    ctx = strategy.SignalContext(
        closes=np.array([100.0] * 30),
        volumes=np.array([1000.0] * 30),
        current_close=100.0,
        current_volume=1000.0,
        rsi_fast=29.0,
        rsi_slow=30.0,
        macd=None,
        bb=None,
        ema_fast=None,
        ema_slow=None,
        momentum=None,
        avg_volume=None,
    )
    assert strategy._signal_dual_rsi_oversold(ctx, strategy.PARAMS) is True
```

**Step 4: Run the regression tests and confirm they fail on the current code**

Run:

```bash
uv run pytest tests/backtest/test_strategy_regression.py -v
```

Expected: FAIL on at least one precedence or helper-parity case, proving the regression before implementation.

**Step 5: Commit**

```bash
git add tests/backtest/test_strategy_regression.py
git commit -m "test: lock legacy strategy behavior in regression tests"
```

### Task 3: Restore Stable Import Semantics In The Backtest Runner

**Files:**
- Modify: `backtest/backtest.py`
- Test: `tests/backtest/test_strategy_regression.py`

**Step 1: Replace dynamic module imports with the original direct imports**

Change:

```python
import importlib
...
prepare = importlib.import_module("prepare")
strategy = importlib.import_module("strategy")
```

to:

```python
import prepare
import strategy
```

Keep the existing `sys.path.insert(0, ...)` behavior intact.

**Step 2: Add a focused import-resolution test**

Extend `tests/backtest/test_strategy_regression.py` with a lightweight smoke test that loads `backtest/backtest.py` and asserts the imported modules expose the expected local APIs:

```python
def test_backtest_runner_uses_local_strategy_module():
    assert hasattr(strategy, "Strategy")
    assert hasattr(strategy, "_resolve_symbol_buy_weight")
```

This is intentionally narrow. The real import regression check is the manual CV evidence from Task 1.

**Step 3: Run the regression tests**

Run:

```bash
uv run pytest tests/backtest/test_strategy_regression.py -v
```

Expected: either all import-related tests pass or the remaining failures are isolated to strategy behavior.

**Step 4: Commit**

```bash
git add backtest/backtest.py tests/backtest/test_strategy_regression.py
git commit -m "fix: restore direct imports in backtest runner"
```

### Task 4: Restore Strategy Decision Parity Without Undoing The Modular Design

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy_regression.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Recreate the original buy-weight precedence exactly**

Prefer the simplest parity-first implementation:

- either replace `SYMBOL_BUY_RULES` with a private helper that copies the original `if`/`elif` chain verbatim
- or reorder and tighten `SYMBOL_BUY_RULES` until every regression test matches the legacy helper

The first version should optimize for certainty, not elegance.

Example parity-first helper:

```python
def _resolve_symbol_buy_weight(symbol: str, bull_flags: dict, market_state: dict, params: dict) -> tuple[float, str | None]:
    pure_reversion_buy = _setup_pure_reversion_buy(bull_flags, params)
    pure_trend_buy = _setup_pure_trend_buy(bull_flags, params)
    strong_reversion_buy = _setup_strong_reversion_buy(bull_flags, params)

    if strong_reversion_buy:
        return params["strong_reversion_position_size"], "strong_reversion_position_size"
    if pure_trend_buy and symbol == "BTC" and ...:
        return params["btc_hot_stall_trend_position_size"], "btc_hot_stall_trend_position_size"
    ...
    return params["position_size"], None
```

Do not re-abstract this helper again until the CV score is restored.

**Step 2: Align extracted helper predicates with the original inline expressions**

Audit and, if needed, copy the original expressions verbatim for:

- `_signal_dual_rsi_oversold()`
- `_setup_pure_reversion_buy()`
- `_setup_strong_reversion_buy()`
- `_setup_pure_trend_buy()`

When in doubt, prefer the `8177d23` inline predicate over `.get(..., False)` convenience.

**Step 3: Preserve the original buy-gating behavior inside `Strategy.on_bar()`**

Verify these gates remain byte-for-byte equivalent in meaning:

- `special_reversion_buy`
- `allow_high_rsi_buy`
- `allow_falling_market_buy`
- `allow_extreme_fall_buy`
- `allow_btc_pure_reversion_buy`
- `allow_eth_strong_reversion_buy`
- `allow_avax_strong_reversion_buy`
- `allow_link_trend_buy`
- `allow_avax_trend_buy`
- `allow_reversion_regime_buy`
- `allow_trend_regime_buy`

If any extracted helper obscures parity, inline the original boolean first and only then rewrap it after verification.

**Step 4: Run the strategy-focused test targets**

Run:

```bash
uv run pytest tests/backtest/test_strategy_regression.py tests/backtest/test_strategy.py -v
```

Expected: PASS. If any test still fails, stop and compare the failing case against the original `8177d23` branch logic before changing anything else.

**Step 5: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py tests/backtest/test_strategy_regression.py
git commit -m "fix: restore legacy strategy decision order"
```

### Task 5: Re-verify Extracted Indicator And Engine Helpers Only If Evidence Points There

**Files:**
- Modify: `backtest/indicators.py`
- Modify: `backtest/prepare.py`
- Test: `tests/backtest/test_prepare.py`
- Test: `tests/backtest/test_strategy_regression.py`

**Step 1: Compare extracted helpers to the original implementations**

Use:

```bash
git diff 8177d23..HEAD -- backtest/prepare.py backtest/indicators.py
```

Focus only on behavior that can affect daily CV:

- `_calc_rsi`, `_calc_ema`, `_calc_macd`, `_calc_bollinger`, `_calc_momentum`, `_calc_average_volume`
- `load_data(..., bar_interval="1d")`
- `run_backtest(..., bar_interval="1d")`
- Sharpe annualization and score calculation

**Step 2: Add failing tests only for proven behavior deltas**

Examples:

```python
def test_daily_interval_defaults_match_pre_refactor_behavior():
    assert prepare.annualization_factor("1d") == pytest.approx(np.sqrt(365.0))

def test_indicator_helper_matches_legacy_rsi_output():
    closes = np.array([...])
    assert indicators._calc_rsi(closes, 14) == pytest.approx(legacy_rsi(closes, 14))
```

Do not add speculative tests. Every new test in this task must correspond to evidence from Task 1 or a concrete diff from `8177d23`.

**Step 3: Revert only the proven behavior delta**

If a helper differs, copy the original implementation exactly into `backtest/indicators.py` or `backtest/prepare.py` while preserving the new module boundaries.

**Step 4: Run the prepare and regression suites**

Run:

```bash
uv run pytest tests/backtest/test_prepare.py tests/backtest/test_strategy_regression.py -v
```

Expected: PASS with no new regressions.

**Step 5: Commit**

```bash
git add backtest/indicators.py backtest/prepare.py tests/backtest/test_prepare.py tests/backtest/test_strategy_regression.py
git commit -m "fix: align extracted helpers with legacy backtest behavior"
```

### Task 6: Perform End-to-End Verification Against The Pre-Refactor Baseline

**Files:**
- Modify: none
- Test: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy_regression.py`
- Test: `tests/backtest/test_prepare.py`

**Step 1: Run the targeted automated suites**

Run:

```bash
uv run pytest tests/backtest/test_strategy.py tests/backtest/test_strategy_regression.py tests/backtest/test_prepare.py -v
```

Expected: PASS.

**Step 2: Run the full CV verification**

Run:

```bash
uv run backtest/backtest.py --mode cv
```

Expected:

```text
cv_score: 4.228733 ± 0.001
```

Also verify folds 2 and 4 are no longer negative-Sharpe outliers.

**Step 3: Run the holdout split verification**

Run:

```bash
uv run backtest/backtest.py --mode single --split test
```

Expected: score is approximately `3.985036`.

**Step 4: Re-run the trade-log diff only if CV still drifts**

Run:

```bash
uv run backtest/backtest.py --mode single --split val > /tmp/trades_candidate.log 2>&1
diff -u /tmp/trades_original.log /tmp/trades_candidate.log | sed -n '1,200p'
```

Expected: either no meaningful decision diff remains, or the first remaining diff points to one last unaligned branch.

**Step 5: Commit**

```bash
git add backtest/backtest.py backtest/strategy.py backtest/prepare.py backtest/indicators.py tests/backtest/test_prepare.py tests/backtest/test_strategy.py tests/backtest/test_strategy_regression.py
git commit -m "fix: restore strategy behavior after modularization refactor"
```
