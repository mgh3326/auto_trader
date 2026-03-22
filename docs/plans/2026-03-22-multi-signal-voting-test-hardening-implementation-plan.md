# Multi-Signal Voting Test Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Strengthen the Phase 3 multi-signal voting tests so they fail on real vote-threshold regressions, while applying only behavior-preserving cleanup to `backtest/strategy.py`.

**Architecture:** Keep the strategy logic intact and target the weak points in `tests/backtest/test_strategy.py`: replace tautological assertions with threshold-boundary checks, make vote reason assertions unconditional, and add explicit tests for hard-exit priority over bear-vote exits. Restrict `backtest/strategy.py` edits to cleanup such as helper extraction, unused method removal, and import/docstring normalization.

**Tech Stack:** Python 3.13, pytest, numpy, pandas, uv

---

### Task 1: Tighten Vote Assembly Assertions

**Files:**
- Modify: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Replace tautological vote assertions**

Update these tests so they verify meaningful conditions instead of `>= 0`:

- `TestVoteAssembly.test_bull_votes_counted_correctly_in_oversold_uptrend`
- `TestVoteAssembly.test_bear_votes_counted_correctly_in_overbought_downtrend`
- `TestVoteAssembly.test_bull_vote_threshold_produces_buy_eligible_setup`
- `TestVoteAssembly.test_bear_votes_for_held_symbol_produces_sell_eligible`

Example direction:

```python
assert result["bull_votes"] >= strategy.MIN_VOTES
assert result["bull_flags"]["dual_rsi_oversold"] is True
assert result["bull_flags"]["volume_above_avg"] is True
```

**Step 2: Keep low-signal fixture meaningful**

For the flat-history case, assert a boundary that matters to decision logic:

```python
assert result["bull_votes"] < strategy.MIN_VOTES
```

instead of a loose condition like `<= 3`.

**Step 3: Run the vote assembly subset**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "VoteAssembly"`

Expected: PASS with strengthened assertions. If a test fails, either the fixture is not deterministic enough or the current vote logic does not match the intended threshold.

**Step 4: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test(backtest): strengthen vote assembly assertions"
```

### Task 2: Add Explicit Threshold Boundary Tests For `on_bar()`

**Files:**
- Modify: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Add deterministic buy-threshold tests**

Patch `Strategy._evaluate_signals` to return controlled vote counts and verify:

```python
def test_no_buy_when_bull_votes_below_threshold(...):
    ...
    mocked = {
        "rsi_fast": 25.0,
        "rsi_slow": 25.0,
        "bull_votes": strategy.MIN_VOTES - 1,
        "bear_votes": 0,
        "bull_flags": {"dual_rsi_oversold": True},
        "bear_flags": {},
    }
```

Expected:

- below threshold => no buy
- at threshold => one buy with `POSITION_SIZE`

**Step 2: Add deterministic sell-threshold tests**

Patch `Strategy._evaluate_signals` for held positions and verify:

- `bear_votes == MIN_SELL_VOTES - 1` => no bear-vote sell
- `bear_votes == MIN_SELL_VOTES` => one sell

Use a held portfolio with no stop-loss, no profitable RSI recovery, and no max-holding trigger so the bear-vote branch is isolated.

**Step 3: Run targeted threshold tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "threshold"`

Expected: PASS with exact branch behavior validated.

**Step 4: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test(backtest): add explicit vote threshold boundary tests"
```

### Task 3: Make Reason String Tests Mandatory

**Files:**
- Modify: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Remove conditional reason assertions**

Replace the current pattern:

```python
if buy_signals:
    assert ...
```

with a test that first requires signal creation and then checks exact reason shape.

Example:

```python
assert len(buy_signals) == 1
assert buy_signals[0].reason.startswith("Bull votes ")
```

**Step 2: Add sell reason formatting coverage**

Add one explicit test for bear-vote sell reason:

```python
assert sell_signals[0].reason.startswith("Bear votes ")
```

Use patched `_evaluate_signals` if necessary to guarantee the branch.

**Step 3: Run reason-format tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "reason"`

Expected: PASS with no conditional skips hidden inside assertions.

**Step 4: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test(backtest): require vote reason formatting in signals"
```

### Task 4: Lock In Hard Exit Priority Over Bear Votes

**Files:**
- Modify: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Add stop-loss priority test**

Create a held portfolio where:

- stop-loss condition is true
- patched `_evaluate_signals` also returns `bear_votes >= MIN_SELL_VOTES`

Verify the emitted sell reason is the stop-loss reason, not the bear-vote reason.

**Step 2: Add RSI recovery priority test**

Create a profitable held portfolio where:

- `rsi_slow >= RSI_EXIT`
- patched `_evaluate_signals` returns `bear_votes >= MIN_SELL_VOTES`

Verify the RSI recovery sell happens first.

**Step 3: Add max-holding priority test**

Create a held portfolio beyond `HOLDING_DAYS` where:

- no stop-loss
- no RSI recovery
- `bear_votes >= MIN_SELL_VOTES`

Verify the max-holding reason wins over bear-vote reason.

**Step 4: Run priority tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "priority or stop_loss or holding or recovery"`

Expected: PASS with branch ordering pinned down.

**Step 5: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test(backtest): pin hard exit priority over bear votes"
```

### Task 5: Apply Behavior-Preserving Cleanup In `strategy.py`

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Remove or justify unused helpers**

Check whether `_get_rsi()` is still used. If not, remove it. If kept, add a comment or test that justifies its existence.

**Step 2: Normalize helper structure**

Allowed cleanup examples:

- move `import pandas as pd` to module scope
- extract repeated vote reason formatting to a private helper such as:

```python
def _format_vote_reason(prefix: str, votes: int, flags: dict[str, bool], limit: int) -> str:
    ...
```

- tighten return type hints for `_evaluate_signals`

Do not change threshold semantics or branch ordering in this task.

**Step 3: Run focused strategy tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v`

Expected: PASS. Any failure indicates cleanup accidentally changed behavior and must be reverted or narrowed.

**Step 4: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "refactor(backtest): clean up voting strategy helpers"
```

### Task 6: Run Full Backtest-Focused Verification

**Files:**
- Modify: none
- Test: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_prepare.py`

**Step 1: Run full backtest pytest suite**

Run: `uv run pytest tests/backtest -v`

Expected: PASS for all backtest tests.

**Step 2: Run smoke backtest if data exists**

Run: `uv run backtest/backtest.py`

Expected:

- If `backtest/data` exists: command completes and prints metrics
- If `backtest/data` does not exist: document that score verification could not be run locally

**Step 3: Record verification outcome**

Capture in review notes:

- whether all tests passed
- whether score/backtest smoke could be run
- any remaining limitations

**Step 4: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "test(backtest): verify multi-signal voting regression coverage"
```
