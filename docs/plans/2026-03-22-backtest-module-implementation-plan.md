# Backtest Module Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a fixed-file backtest module for Upbit spot crypto daily bars, including data backfill, deterministic engine, initial RSI strategy, and benchmark strategies.

**Architecture:** Create a new `backtest/` package-like directory with fixed engine files (`prepare.py`, `backtest.py`), one mutable strategy file (`strategy.py`), and a separate data backfill script. Keep experiment reproducibility by fixing the backtest universe and split boundaries inside `prepare.py`, while allowing broader market data collection in `fetch_data.py`.

**Tech Stack:** Python 3.13, uv, pandas, numpy, httpx, pyarrow, pytest, Ruff

---

### Task 1: Add Parquet Dependency And Ignore Rules

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitignore`
- Test: none

**Step 1: Add `pyarrow` dependency**

Update `dependencies` in `pyproject.toml`:

```toml
"pyarrow>=18.0.0,<19.0.0",
```

Place it near `pandas` because it is a runtime data dependency for this module.

**Step 2: Add backtest ignore entries**

Append to `.gitignore`:

```gitignore
backtest/data/
backtest/results.tsv
backtest/run.log
```

**Step 3: Verify dependency and ignore entries**

Run: `rg -n "pyarrow|backtest/data|backtest/results.tsv|backtest/run.log" pyproject.toml .gitignore`

Expected: one `pyarrow` dependency match and three `.gitignore` matches.

**Step 4: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "build: add parquet support for backtest module"
```

### Task 2: Scaffold Backtest Directory

**Files:**
- Create: `backtest/prepare.py`
- Create: `backtest/backtest.py`
- Create: `backtest/strategy.py`
- Create: `backtest/fetch_data.py`
- Create: `backtest/program.md`
- Create: `backtest/benchmarks/buy_and_hold.py`
- Create: `backtest/benchmarks/random_baseline.py`
- Test: none

**Step 1: Create empty module skeleton files**

Each Python file should have a top-level docstring and minimal placeholders only. `program.md` should contain a placeholder header. Do not write full logic yet.

Minimal placeholder example:

```python
"""Backtest engine."""
```

**Step 2: Verify file layout**

Run: `find backtest -maxdepth 2 -type f | sort`

Expected:

```text
backtest/backtest.py
backtest/benchmarks/buy_and_hold.py
backtest/benchmarks/random_baseline.py
backtest/fetch_data.py
backtest/prepare.py
backtest/program.md
backtest/strategy.py
```

**Step 3: Commit**

```bash
git add backtest
git commit -m "chore: scaffold backtest module layout"
```

### Task 3: Write Loader And Engine Tests First

**Files:**
- Create: `tests/backtest/test_prepare.py`
- Test: `tests/backtest/test_prepare.py`

**Step 1: Write split-loading tests**

Add tests that create temporary Parquet fixtures and assert `load_data("val")`:

- reads only `DEFAULT_SYMBOLS`
- applies split date boundaries
- returns frames sorted ascending

Example structure:

```python
def test_load_data_filters_symbols_and_dates(tmp_path, monkeypatch):
    ...
    data = prepare.load_data("val")
    assert set(data) == {"BTC", "ETH"}
    assert data["BTC"]["date"].tolist() == ["2025-04-01", "2025-04-02"]
```

**Step 2: Write execution-cost tests**

Add tests for:

- buy execution with slippage and fee
- sell execution with slippage and fee
- target-weight buy sizing
- partial sell sizing

Example assertion:

```python
assert result.trade_log[0]["fee"] > 0
assert result.trade_log[0]["price"] == pytest.approx(101.0)
```

**Step 3: Write equity and metric tests**

Add tests for:

- total return
- max drawdown
- sharpe non-NaN behavior
- score penalty when `num_trades < 10`

**Step 4: Run tests to verify failure**

Run: `uv run pytest tests/backtest/test_prepare.py -v`

Expected: FAIL because `backtest.prepare` does not yet provide the tested API.

**Step 5: Commit**

```bash
git add tests/backtest/test_prepare.py
git commit -m "test: add failing tests for backtest engine"
```

### Task 4: Implement `prepare.py`

**Files:**
- Modify: `backtest/prepare.py`
- Test: `tests/backtest/test_prepare.py`

**Step 1: Define constants and dataclasses**

Add:

- `INITIAL_CAPITAL`
- `TRADING_FEE`
- `SLIPPAGE_BPS`
- `LOOKBACK_BARS`
- `BAR_INTERVAL`
- `DEFAULT_SYMBOLS`
- split date constants
- `BarData`, `Signal`, `PortfolioState`, `BacktestResult`

Include `position_dates` in `PortfolioState`.

**Step 2: Implement `load_data()`**

Requirements:

- use `Path(__file__).resolve().parent / "data"`
- map `BTC` to `KRW-BTC.parquet`
- read Parquet with pandas
- validate required columns
- filter to split dates
- keep only non-empty frames

Skeleton:

```python
def load_data(split: str = "val") -> dict[str, pd.DataFrame]:
    start, end = _resolve_split_dates(split)
    data = {}
    for symbol in DEFAULT_SYMBOLS:
        path = DATA_DIR / f"KRW-{symbol}.parquet"
        ...
    return data
```

**Step 3: Implement order execution helpers**

Write internal helpers for:

- execution price calculation
- buy order sizing from target weight
- sell order sizing from current quantity ratio
- average price updates
- realized PnL updates

Keep these helpers pure where possible.

**Step 4: Implement `run_backtest()`**

Requirements:

- build unified date sequence from loaded symbol frames
- create per-day `bar_data`
- construct `PortfolioState`
- call `strategy.on_bar(...)`
- execute signals sequentially
- update equity curve daily
- accumulate trade log and round-trip stats

Keep missing-symbol dates non-fatal.

**Step 5: Implement metric helpers**

Add helpers for:

- daily returns
- sharpe
- max drawdown
- win rate
- profit factor
- average holding days

**Step 6: Implement `compute_score()`**

Use the approved penalty formula exactly.

**Step 7: Run engine tests**

Run: `uv run pytest tests/backtest/test_prepare.py -v`

Expected: PASS

**Step 8: Run style checks for the file**

Run: `uv run ruff check backtest/prepare.py tests/backtest/test_prepare.py`

Expected: PASS

**Step 9: Commit**

```bash
git add backtest/prepare.py tests/backtest/test_prepare.py
git commit -m "feat: add deterministic backtest engine"
```

### Task 5: Write Fetch Script Tests First

**Files:**
- Create: `tests/backtest/test_fetch_data.py`
- Test: `tests/backtest/test_fetch_data.py`

**Step 1: Write market-selection tests**

Test:

- KRW-only filtering
- top-N slicing
- `--symbols BTC ETH` normalization to `KRW-BTC`, `KRW-ETH`

**Step 2: Write candle-normalization tests**

Test conversion from Upbit API rows to target schema:

```python
assert df.columns.tolist() == ["date", "open", "high", "low", "close", "volume", "value"]
assert df["date"].tolist() == ["2026-03-20", "2026-03-21"]
```

**Step 3: Write merge/dedupe tests**

Test incremental behavior:

- existing parquet rows
- fetched replacement window
- merged result keeps latest row per date
- final rows sorted ascending

**Step 4: Run tests to verify failure**

Run: `uv run pytest tests/backtest/test_fetch_data.py -v`

Expected: FAIL because fetch helpers are not implemented yet.

**Step 5: Commit**

```bash
git add tests/backtest/test_fetch_data.py
git commit -m "test: add failing tests for backtest data backfill"
```

### Task 6: Implement `fetch_data.py`

**Files:**
- Modify: `backtest/fetch_data.py`
- Test: `tests/backtest/test_fetch_data.py`

**Step 1: Add CLI parsing**

Support:

- no args
- `--symbols BTC ETH`
- `--days 365`
- `--top-n 100`

Suggested parser:

```python
parser = argparse.ArgumentParser()
parser.add_argument("--symbols", nargs="*")
parser.add_argument("--days", type=int, default=730)
parser.add_argument("--top-n", type=int, default=100)
```

**Step 2: Add market fetch helper**

Use `httpx` against `/v1/market/all` and filter `market.startswith("KRW-")`.

**Step 3: Add candle pagination helper**

Requirements:

- endpoint `/v1/candles/days`
- max `count=200`
- use `to` cursor for pagination
- insert `time.sleep(0.1)` between requests

**Step 4: Add normalization and persistence helpers**

Implement:

- API row normalization
- recent-window re-fetch for incremental updates
- merge + dedupe + ascending sort
- parquet write

**Step 5: Add `main()` orchestration**

For each market:

- fetch candles
- merge with existing parquet if present
- save result
- print simple progress line

**Step 6: Run fetch tests**

Run: `uv run pytest tests/backtest/test_fetch_data.py -v`

Expected: PASS

**Step 7: Run style checks**

Run: `uv run ruff check backtest/fetch_data.py tests/backtest/test_fetch_data.py`

Expected: PASS

**Step 8: Commit**

```bash
git add backtest/fetch_data.py tests/backtest/test_fetch_data.py
git commit -m "feat: add upbit daily candle backfill script"
```

### Task 7: Write Strategy Tests First

**Files:**
- Create: `tests/backtest/test_strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Write RSI helper tests**

Test:

- RSI returns finite value with enough history
- insufficient history returns no signal path

**Step 2: Write buy-signal tests**

Test:

- RSI below oversold threshold
- symbol not already held
- current position count below max
- emits `buy` with configured weight

**Step 3: Write sell-signal tests**

Test:

- RSI above overbought emits full sell
- holding period exceeded plus profitable position emits full sell
- unprofitable aged position does not sell on holding-period rule

**Step 4: Run tests to verify failure**

Run: `uv run pytest tests/backtest/test_strategy.py -v`

Expected: FAIL because `Strategy` is not fully implemented.

**Step 5: Commit**

```bash
git add tests/backtest/test_strategy.py
git commit -m "test: add failing tests for backtest strategy"
```

### Task 8: Implement `strategy.py`

**Files:**
- Modify: `backtest/strategy.py`
- Test: `tests/backtest/test_strategy.py`

**Step 1: Add initial RSI strategy constants**

Include:

- `RSI_PERIOD = 14`
- `RSI_OVERSOLD = 30`
- `RSI_OVERBOUGHT = 70`
- `MAX_POSITIONS = 5`
- `POSITION_SIZE = 0.15`
- `HOLDING_DAYS = 7`

**Step 2: Implement RSI calculation**

Use pandas/numpy only. Keep it local to the strategy file.

Example:

```python
def _calc_rsi(self, closes: np.ndarray, period: int = 14) -> float:
    ...
```

**Step 3: Implement `on_bar()`**

Rules:

- skip symbols without enough history
- buy on RSI oversold when not already held and slots remain
- sell full on RSI overbought
- else sell full when holding days exceeded and current close > avg price

Use `portfolio.position_dates` instead of internal `entry_dates`.

**Step 4: Run strategy tests**

Run: `uv run pytest tests/backtest/test_strategy.py -v`

Expected: PASS

**Step 5: Run style checks**

Run: `uv run ruff check backtest/strategy.py tests/backtest/test_strategy.py`

Expected: PASS

**Step 6: Commit**

```bash
git add backtest/strategy.py tests/backtest/test_strategy.py
git commit -m "feat: add initial RSI backtest strategy"
```

### Task 9: Implement Benchmark Strategies

**Files:**
- Modify: `backtest/benchmarks/buy_and_hold.py`
- Modify: `backtest/benchmarks/random_baseline.py`
- Test: extend `tests/backtest/test_strategy.py` or create `tests/backtest/test_benchmarks.py`

**Step 1: Add buy-and-hold benchmark**

Rules:

- first day only
- equally weighted buys across incoming symbols
- no further actions

**Step 2: Add random baseline benchmark**

Rules:

- fixed seed
- low-frequency signals
- no shorting
- avoid generating invalid sell signals for unheld names

**Step 3: Add benchmark contract tests**

Test:

- both classes expose `on_bar`
- buy-and-hold buys once
- random baseline is deterministic under same seed

**Step 4: Run benchmark tests**

Run: `uv run pytest tests/backtest/test_strategy.py tests/backtest/test_benchmarks.py -v`

Expected: PASS

**Step 5: Commit**

```bash
git add backtest/benchmarks tests/backtest/test_benchmarks.py
git commit -m "feat: add benchmark strategies for backtest module"
```

### Task 10: Finalize Fixed Entry Point And Program Guide

**Files:**
- Modify: `backtest/backtest.py`
- Modify: `backtest/program.md`
- Test: smoke run only

**Step 1: Implement `backtest.py` exactly as fixed runner**

Requirements:

- keep imports local and simple
- no benchmark CLI
- print approved metrics

**Step 2: Write `program.md`**

Document:

- `strategy.py` only is mutable
- `prepare.py`, `backtest.py`, `fetch_data.py` are fixed
- allowed libraries
- loop for score comparison
- placeholder score section for post-implementation update

**Step 3: Run lint**

Run: `uv run ruff check backtest/backtest.py`

Expected: PASS

**Step 4: Commit**

```bash
git add backtest/backtest.py backtest/program.md
git commit -m "docs: add backtest runner guide"
```

### Task 11: End-To-End Verification With Live Data

**Files:**
- Modify: `backtest/program.md` if score needs to be recorded
- Test: manual runtime verification

**Step 1: Backfill sample live data**

Run: `uv run backtest/fetch_data.py --symbols BTC ETH SOL --days 365`

Expected:

- Parquet files created under `backtest/data/`
- no uncaught exceptions

**Step 2: Run fixed backtest**

Run: `uv run backtest/backtest.py`

Expected:

- bars loaded message
- `score`, `sharpe`, `total_return_pct`, `max_drawdown_pct`, `num_trades` lines printed

**Step 3: Record first baseline score**

Update `backtest/program.md` with the observed initial score and current strategy summary.

**Step 4: Run targeted regression suite**

Run: `uv run pytest tests/backtest -v`

Expected: PASS

**Step 5: Run final lint**

Run: `uv run ruff check backtest tests/backtest`

Expected: PASS

**Step 6: Commit**

```bash
git add backtest/program.md tests/backtest
git commit -m "test: verify backtest module with live upbit data"
```

### Task 12: Prepare PR

**Files:**
- No code changes required

**Step 1: Review final diff**

Run: `git status --short && git log --oneline -5`

Expected: clean worktree and recent backtest-related commits visible.

**Step 2: Summarize operator commands**

Prepare final notes with:

- `uv run backtest/fetch_data.py --symbols BTC ETH SOL --days 365`
- `uv run backtest/backtest.py`
- known limitations for Phase 1

**Step 3: Open PR**

Use your normal repository workflow after all verification passes.
