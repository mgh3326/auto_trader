# Multi-Signal Voting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend `backtest/strategy.py` from dual-RSI entry logic to a parameterized multi-signal voting strategy while preserving existing hard risk controls and keeping the backtest engine unchanged.

**Architecture:** Keep all new behavior inside `backtest/strategy.py`. Add small indicator helper functions for EMA, MACD, Bollinger, momentum, and volume statistics, then compute boolean buy/sell votes inside `Strategy.on_bar()`. Preserve stop-loss, RSI recovery exit, and max-holding exits as higher-priority branches, and use vote thresholds only for entry plus optional secondary exit logic.

**Tech Stack:** Python 3.13, numpy, pandas, pytest, uv

---

### Task 1: Realign Strategy Tests With The Current Contract

**Files:**
- Modify: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Replace stale mock-based tests with current API assumptions**

Remove tests that patch nonexistent helpers like `_get_rsi_from_history`. Keep or rewrite them to target the actual helpers and `Strategy.on_bar()` behavior.

Target coverage to preserve:

- insufficient history returns no signals
- max positions prevents new buys
- held symbols do not get duplicate buys
- profitable RSI recovery still sells

**Step 2: Add helper builders for richer histories**

Create or extend local fixtures/helpers so tests can build deterministic `history` frames with:

- custom close series
- custom volume series
- explicit dates

Example helper shape:

```python
def _make_history(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    ...
```

**Step 3: Run the strategy tests to see the current baseline**

Run: `uv run pytest tests/backtest/test_strategy.py -v`

Expected: current tests either fail because of stale mocks or pass only the legacy RSI behavior. Record this before changing strategy logic.

**Step 4: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test(backtest): realign strategy tests with current contract"
```

### Task 2: Add Indicator Helper Functions In `strategy.py`

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Add new tunable constants near the existing strategy constants**

Add:

```python
MIN_VOTES = 3
MIN_SELL_VOTES = 2
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2.0
EMA_FAST = 10
EMA_SLOW = 30
MOMENTUM_PERIOD = 10
VOLUME_LOOKBACK = 20
VOLUME_THRESHOLD = 1.5
```

Keep existing `RSI_*`, `POSITION_SIZE`, `STOP_LOSS_PCT`, `COOLDOWN_DAYS`, and `HOLDING_DAYS` unchanged.

**Step 2: Add small pure helpers for indicator calculations**

Implement:

- `_calc_ema(closes, span) -> np.ndarray | None`
- `_calc_macd(closes, fast, slow, signal) -> tuple[float, float, float] | None`
- `_calc_bollinger(closes, period, std_mult) -> tuple[float, float, float] | None`
- `_calc_momentum(closes, period) -> float | None`
- `_calc_average_volume(volumes, lookback) -> float | None`

Rules:

- return `None` when warmup is insufficient
- avoid pandas rolling helpers inside hot loops unless simpler than manual numpy
- keep function inputs/outputs simple so tests can hit them directly

**Step 3: Add direct helper tests before wiring them into `on_bar()`**

Add tests like:

```python
def test_calc_ema_tracks_uptrend():
    closes = np.array([1.0, 2.0, 3.0, 4.0])
    ema = strategy._calc_ema(closes, 3)
    assert ema is not None
    assert ema[-1] > ema[0]
```

Also add failure-path tests for insufficient history.

**Step 4: Run targeted tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "calc_ or insufficient"`

Expected: new indicator helper tests pass.

**Step 5: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "feat(backtest): add indicator helpers for voting strategy"
```

### Task 3: Build Bull/Bear Vote Assembly Helpers

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Add a per-symbol signal evaluation helper**

Create a private method or function that extracts arrays from `bar.history`, computes indicators once, and returns a structured result:

```python
{
    "rsi_fast": ...,
    "rsi_slow": ...,
    "bull_votes": 4,
    "bear_votes": 2,
    "bull_flags": {...},
    "bear_flags": {...},
}
```

This keeps `on_bar()` readable and prevents duplicated indicator work across buy/sell branches.

**Step 2: Define initial vote composition**

Bull votes:

- dual RSI oversold
- MACD histogram positive
- close below Bollinger lower band
- fast EMA above slow EMA
- close above close[-MOMENTUM_PERIOD]
- volume above mean * threshold

Bear votes:

- MACD histogram negative
- close above Bollinger upper band
- fast EMA below slow EMA
- close below close[-MOMENTUM_PERIOD]
- slow RSI above a sell threshold or recovery threshold

Do not include stop-loss or holding-period in the vote count; they remain hard exits.

**Step 3: Add tests for vote counting**

Write deterministic tests that patch the helper layer or craft exact histories to assert:

- `bull_votes >= MIN_VOTES` produces buy-eligible setup
- `bull_votes < MIN_VOTES` does not
- `bear_votes >= MIN_SELL_VOTES` becomes sell-eligible only for held symbols

Keep at least one test asserting the reason string mentions vote count or triggered signals.

**Step 4: Run vote tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v -k "vote or buy or sell"`

Expected: vote-counting behavior is stable before full `on_bar()` integration.

**Step 5: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "feat(backtest): add bull and bear vote assembly"
```

### Task 4: Integrate Voting Into `Strategy.on_bar()`

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Preserve sell-side hard exit priority**

Keep this order at the top of the held-position branch:

1. stop-loss
2. profitable RSI recovery exit
3. max holding days exit
4. bear-vote exit

This avoids weakening the existing defensive behavior that produced the current `+0.74` baseline.

**Step 2: Replace legacy buy condition with vote threshold**

Change:

```python
both_oversold = ...
if both_oversold:
    ...
```

to:

```python
if bull_votes >= MIN_VOTES:
    ...
```

The buy signal reason should include enough context to debug tuning, for example:

```python
reason=f"Bull votes {bull_votes}/{TOTAL_BULL_SIGNALS}: rsi, macd, momentum"
```

**Step 3: Add optional bear-vote sell reason**

When `bear_votes >= MIN_SELL_VOTES`, emit:

```python
prepare.Signal(
    symbol=symbol,
    action="sell",
    weight=1.0,
    reason=f"Bear votes {bear_votes}: macd, ema, momentum",
)
```

Do not trigger this branch if an earlier hard exit already fired.

**Step 4: Run full strategy test suite**

Run: `uv run pytest tests/backtest/test_strategy.py -v`

Expected: all strategy tests pass with the new voting behavior.

**Step 5: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "feat(backtest): integrate multi-signal voting into strategy"
```

### Task 5: Verify Engine Compatibility And Baseline Quality

**Files:**
- Modify: none
- Test: `tests/backtest/test_prepare.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Run backtest-focused pytest suite**

Run: `uv run pytest tests/backtest -v`

Expected: all backtest tests pass. Any failure in `test_prepare.py` means the strategy changes leaked an engine contract assumption and must be fixed in `strategy.py`, not in engine files.

**Step 2: Run the fixed backtest entry point**

Run: `uv run backtest/backtest.py`

Expected:

- script completes without editing `prepare.py` or `backtest.py`
- score is reported to stdout
- trade reasons show vote-based entry or exit wording

Capture:

- score
- sharpe
- max drawdown
- trade count

**Step 3: Compare against the current baseline**

Success target:

- primary: score `> +0.74`
- secondary: no catastrophic trade collapse from over-filtering

If score regresses, note which threshold or signal seems most likely responsible before tuning.

**Step 4: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "feat(backtest): verify multi-signal voting strategy baseline"
```

### Task 6: Prepare Autoresearch Follow-Up Knobs

**Files:**
- Modify: `backtest/strategy.py`
- Test: none

**Step 1: Review constant layout for tuneability**

Ensure all Phase 3 knobs sit in one contiguous block near the top of `strategy.py`, including:

- vote thresholds
- indicator periods
- band multipliers
- volume threshold

Avoid magic numbers inside helper bodies or `on_bar()`.

**Step 2: Normalize reason strings and inline comments**

Keep comments sparse but make reason strings machine-comparable enough for experiment logs. Good examples:

- `"Bull votes 4/6: rsi, macd, bb, volume"`
- `"Bear votes 3/5: macd, ema, momentum"`

**Step 3: Run one final smoke backtest**

Run: `uv run backtest/backtest.py`

Expected: same metrics shape as Task 5, no syntax or runtime regressions after cleanup.

**Step 4: Commit**

```bash
git add backtest/strategy.py
git commit -m "chore(backtest): prepare voting parameters for autoresearch"
```
