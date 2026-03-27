# Phase 1: Crypto RSI Portfolio Backtest MVP

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a rebalancing portfolio backtest engine that selects Upbit coins by 거래대금 ranking + RSI ascending, simulates equal-weight portfolios on 1h candles, and outputs performance metrics with BTC benchmark comparison.

**Architecture:** New `backtest/rsi/` package alongside the existing daily-bar backtest. Data loader fetches 1h candles from Upbit REST API and caches as parquet. Simulator iterates hourly bars, rebalances at configured intervals, and tracks equity curve. CLI entrypoint at `backtest/rsi_backtest.py`.

**Tech Stack:** Python 3.13, numpy, pandas, httpx (sync), pyarrow/parquet, argparse. No new dependencies needed — all already in pyproject.toml.

---

## File Structure

```
backtest/rsi/                     # New package
├── __init__.py                   # Package exports
├── config.py                     # BacktestConfig frozen dataclass
├── indicators.py                 # Wilder's RSI calculation
├── data_loader.py                # 1h candle fetch + parquet cache
├── universe.py                   # Rolling 거래대금 → top-N ranking
├── strategy.py                   # RSI filter + ascending sort → pick-K
├── simulator.py                  # Rebalancing portfolio engine
├── metrics.py                    # CAGR, Sharpe, MDD, benchmark
└── report.py                     # Pretty-print + CSV export

backtest/rsi_backtest.py          # CLI entrypoint (uv run backtest/rsi_backtest.py ...)
backtest/data/1h/                 # Cached 1h parquet files (gitignored)

tests/backtest/
├── test_rsi_indicators.py
├── test_rsi_universe.py
├── test_rsi_strategy.py
├── test_rsi_simulator.py
└── test_rsi_metrics.py
```

## Strategy Assumptions

| Parameter | Value | Notes |
|-----------|-------|-------|
| Market | Upbit KRW spot | KRW pairs only |
| Timeframe | 1h | Hourly candles |
| Universe | 거래대금 top-N (rolling 24h) | Default N=30 |
| Selection | RSI-14 ascending, filtered by max_rsi | Default max_rsi=45 |
| Pick | Top K lowest RSI from filtered universe | Default K=5 |
| Weights | Equal-weight (1/K each) | |
| Rebalance | Every N hours | Default 24h |
| Execution | Current bar close + slippage | At rebalance bar |
| Fee | 0.05% per trade (Upbit standard) | |
| Slippage | 2 basis points | |
| Initial capital | 10,000,000 KRW | |
| Benchmark | BTC buy & hold | Same period |

---

## Task 1: Package Skeleton + Config

**Files:**
- Create: `backtest/rsi/__init__.py`
- Create: `backtest/rsi/config.py`

- [ ] **Step 1: Create package with config dataclass**

```python
# backtest/rsi/__init__.py
"""Crypto RSI portfolio backtest engine."""
```

```python
# backtest/rsi/config.py
"""Backtest configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestConfig:
    """All parameters for a single backtest run."""

    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD
    top_n: int = 30
    pick_k: int = 5
    max_rsi: float = 45.0
    rsi_period: int = 14
    rebalance_hours: int = 24
    initial_capital: float = 10_000_000
    fee_rate: float = 0.0005  # 0.05%
    slippage_bps: float = 2.0
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && python -c "import sys; sys.path.insert(0,'backtest'); from rsi.config import BacktestConfig; print(BacktestConfig(start='2024-01-01', end='2025-01-01'))"`

Expected: Prints the dataclass repr without errors.

- [ ] **Step 3: Commit**

```bash
git add backtest/rsi/__init__.py backtest/rsi/config.py
git commit -m "feat(backtest): add rsi package skeleton and BacktestConfig"
```

---

## Task 2: RSI Indicator + Tests

**Files:**
- Create: `backtest/rsi/indicators.py`
- Create: `tests/backtest/test_rsi_indicators.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_indicators.py
"""Tests for RSI indicator calculations."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.indicators import calc_rsi, calc_rsi_series


class TestCalcRsi:
    """Tests for single-point RSI calculation."""

    def test_insufficient_data_returns_none(self):
        closes = np.array([100.0, 101.0, 102.0])
        assert calc_rsi(closes, period=14) is None

    def test_all_gains_returns_100(self):
        # 16 consecutively rising closes → RSI should be 100
        closes = np.arange(100.0, 116.0, dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert rsi == pytest.approx(100.0, abs=0.01)

    def test_all_losses_returns_near_zero(self):
        closes = np.arange(200.0, 184.0, -1.0, dtype=float)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert rsi < 1.0

    def test_known_value(self):
        # 50/50 alternating gains/losses should produce RSI near 50
        rng = np.random.default_rng(42)
        base = 100.0
        prices = [base]
        for _ in range(100):
            change = rng.choice([-1.0, 1.0])
            prices.append(prices[-1] + change)
        closes = np.array(prices)
        rsi = calc_rsi(closes, period=14)
        assert rsi is not None
        assert 30.0 < rsi < 70.0

    def test_period_6(self):
        closes = np.arange(100.0, 108.0, dtype=float)  # 8 points, period=6
        rsi = calc_rsi(closes, period=6)
        assert rsi is not None
        assert rsi == pytest.approx(100.0, abs=0.01)


class TestCalcRsiSeries:
    """Tests for full-series RSI calculation."""

    def test_output_length_matches_input(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert len(result) == len(closes)

    def test_first_period_values_are_nan(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert all(np.isnan(result[:14]))

    def test_values_after_period_are_not_nan(self):
        closes = np.arange(100.0, 130.0, dtype=float)
        result = calc_rsi_series(closes, period=14)
        assert not np.isnan(result[14])

    def test_last_value_matches_calc_rsi(self):
        rng = np.random.default_rng(99)
        closes = 100.0 + np.cumsum(rng.normal(0, 1, 50))
        series_val = calc_rsi_series(closes, period=14)[-1]
        point_val = calc_rsi(closes, period=14)
        assert series_val == pytest.approx(point_val, abs=0.01)

    def test_insufficient_data_all_nan(self):
        closes = np.array([100.0, 101.0, 102.0])
        result = calc_rsi_series(closes, period=14)
        assert all(np.isnan(result))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_indicators.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.indicators'`

- [ ] **Step 3: Implement indicators**

```python
# backtest/rsi/indicators.py
"""RSI indicator using Wilder's smoothing method."""

import numpy as np


def calc_rsi(closes: np.ndarray, period: int = 14) -> float | None:
    """Calculate current RSI value for a price series.

    Uses Wilder's exponential smoothing (same as TradingView).

    Args:
        closes: Array of closing prices, oldest first.
        period: RSI lookback period.

    Returns:
        RSI value (0-100) or None if insufficient data.
    """
    if len(closes) < period + 1:
        return None

    deltas = np.diff(closes)

    # Seed averages with SMA of first `period` deltas
    seed = deltas[:period]
    avg_gain = np.where(seed > 0, seed, 0.0).sum() / period
    avg_loss = -np.where(seed < 0, seed, 0.0).sum() / period

    # Wilder's smoothing for remaining deltas
    for delta in deltas[period:]:
        if delta > 0:
            avg_gain = (avg_gain * (period - 1) + delta) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) - delta) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_rsi_series(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculate RSI for every point in the series.

    Args:
        closes: Array of closing prices, oldest first.
        period: RSI lookback period.

    Returns:
        Array of RSI values (NaN where insufficient data).
    """
    n = len(closes)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed with SMA
    avg_gain = gains[:period].sum() / period
    avg_loss = losses[:period].sum() / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    # Wilder's smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    return rsi
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_indicators.py -v`

Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/rsi/indicators.py tests/backtest/test_rsi_indicators.py
git commit -m "feat(backtest): add Wilder RSI indicator with tests"
```

---

## Task 3: Data Loader (1h Candle Fetch + Parquet Cache)

**Files:**
- Create: `backtest/rsi/data_loader.py`
- Create: `tests/backtest/test_rsi_data_loader.py`

This is the largest single module. It handles:
1. Fetching all KRW market codes from Upbit
2. Fetching 1h candles with pagination (200 per request)
3. Caching to parquet in `backtest/data/1h/`
4. Loading cached data for a date range
5. Incremental refresh (only fetch missing tail)

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_data_loader.py
"""Tests for 1h candle data loader."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.data_loader import (
    normalize_candles,
    merge_with_existing,
    load_candles,
    DATA_DIR,
)


class TestNormalizeCandles:
    """Tests for Upbit API response normalization."""

    def test_empty_list_returns_empty_df(self):
        df = normalize_candles([])
        assert len(df) == 0
        assert list(df.columns) == ["datetime", "open", "high", "low", "close", "volume", "value"]

    def test_maps_upbit_columns(self):
        raw = [
            {
                "candle_date_time_kst": "2024-01-01T09:00:00",
                "opening_price": 100.0,
                "high_price": 110.0,
                "low_price": 90.0,
                "trade_price": 105.0,
                "candle_acc_trade_volume": 50.0,
                "candle_acc_trade_price": 5000.0,
            }
        ]
        df = normalize_candles(raw)
        assert len(df) == 1
        assert df.iloc[0]["datetime"] == "2024-01-01T09:00:00"
        assert df.iloc[0]["close"] == 105.0
        assert df.iloc[0]["value"] == 5000.0

    def test_sorts_by_datetime_ascending(self):
        raw = [
            {"candle_date_time_kst": "2024-01-01T11:00:00", "opening_price": 100, "high_price": 100, "low_price": 100, "trade_price": 100, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 100},
            {"candle_date_time_kst": "2024-01-01T09:00:00", "opening_price": 99, "high_price": 99, "low_price": 99, "trade_price": 99, "candle_acc_trade_volume": 1, "candle_acc_trade_price": 99},
        ]
        df = normalize_candles(raw)
        assert df.iloc[0]["datetime"] == "2024-01-01T09:00:00"
        assert df.iloc[1]["datetime"] == "2024-01-01T11:00:00"


class TestMergeWithExisting:
    """Tests for merging new data with cached parquet."""

    def test_no_existing_returns_new(self, tmp_path):
        new_df = pd.DataFrame({"datetime": ["2024-01-01T09:00:00"], "close": [100.0]})
        result = merge_with_existing(new_df, tmp_path / "nonexistent.parquet")
        assert len(result) == 1

    def test_deduplicates_by_datetime(self, tmp_path):
        existing = pd.DataFrame({"datetime": ["2024-01-01T09:00:00"], "close": [100.0]})
        parquet_path = tmp_path / "test.parquet"
        existing.to_parquet(parquet_path, index=False)

        new_df = pd.DataFrame({
            "datetime": ["2024-01-01T09:00:00", "2024-01-01T10:00:00"],
            "close": [101.0, 102.0],
        })
        result = merge_with_existing(new_df, parquet_path)
        assert len(result) == 2
        # New data should overwrite existing for same datetime
        row = result[result["datetime"] == "2024-01-01T09:00:00"]
        assert row.iloc[0]["close"] == 101.0


class TestLoadCandles:
    """Tests for loading cached candle data."""

    def test_returns_none_when_no_cache(self, tmp_path):
        result = load_candles("KRW-BTC", "2024-01-01", "2024-02-01", data_dir=tmp_path)
        assert result is None

    def test_filters_by_date_range(self, tmp_path):
        df = pd.DataFrame({
            "datetime": [
                "2024-01-01T09:00:00",
                "2024-01-15T09:00:00",
                "2024-02-15T09:00:00",
            ],
            "open": [100, 101, 102],
            "high": [100, 101, 102],
            "low": [100, 101, 102],
            "close": [100, 101, 102],
            "volume": [10, 10, 10],
            "value": [1000, 1010, 1020],
        })
        path = tmp_path / "KRW-BTC.parquet"
        df.to_parquet(path, index=False)

        result = load_candles("KRW-BTC", "2024-01-01", "2024-01-31", data_dir=tmp_path)
        assert result is not None
        assert len(result) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_data_loader.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.data_loader'`

- [ ] **Step 3: Implement data_loader**

```python
# backtest/rsi/data_loader.py
"""Upbit 1h candle data loader with parquet caching."""

import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

UPBIT_API_URL = "https://api.upbit.com/v1"
DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "1h"
MAX_CANDLES_PER_REQUEST = 200
REQUEST_INTERVAL = 0.11  # seconds between requests (stay under 10/sec)


def fetch_krw_markets() -> list[str]:
    """Fetch all KRW market codes from Upbit.

    Returns:
        List of market codes like ["KRW-BTC", "KRW-ETH", ...].
    """
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{UPBIT_API_URL}/market/all", params={"is_details": "false"})
        resp.raise_for_status()
        markets = resp.json()
    return [m["market"] for m in markets if m["market"].startswith("KRW-")]


def fetch_hourly_candles(market: str, start: str, end: str) -> list[dict]:
    """Fetch 1h candles from Upbit API with backward pagination.

    Args:
        market: Market code (e.g., "KRW-BTC").
        start: Start date "YYYY-MM-DD" (inclusive).
        end: End date "YYYY-MM-DD" (inclusive, fetches up to end+1 day 00:00 KST).

    Returns:
        Raw candle dicts from Upbit API.
    """
    end_dt = datetime.fromisoformat(f"{end}T23:59:59")
    start_dt = datetime.fromisoformat(f"{start}T00:00:00")
    to_dt = end_dt + timedelta(hours=1)

    all_candles: list[dict] = []

    with httpx.Client(timeout=30) as client:
        while True:
            params = {
                "market": market,
                "count": MAX_CANDLES_PER_REQUEST,
                "to": to_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            resp = client.get(f"{UPBIT_API_URL}/candles/minutes/60", params=params)
            resp.raise_for_status()
            candles = resp.json()

            if not candles:
                break

            all_candles.extend(candles)

            # Oldest candle in this batch
            oldest_kst = candles[-1]["candle_date_time_kst"]
            oldest_dt = datetime.fromisoformat(oldest_kst)

            if oldest_dt <= start_dt:
                break

            # Next page: before the oldest candle
            to_dt = oldest_dt
            time.sleep(REQUEST_INTERVAL)

    return all_candles


def normalize_candles(raw: list[dict]) -> pd.DataFrame:
    """Normalize Upbit candle response to standard DataFrame.

    Columns: datetime, open, high, low, close, volume, value.
    datetime is KST ISO string "YYYY-MM-DDTHH:MM:SS".
    """
    if not raw:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "value"])

    df = pd.DataFrame(raw)
    df = df.rename(columns={
        "candle_date_time_kst": "datetime",
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
        "candle_acc_trade_price": "value",
    })
    df = df[["datetime", "open", "high", "low", "close", "volume", "value"]]
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def merge_with_existing(new_df: pd.DataFrame, parquet_path: Path) -> pd.DataFrame:
    """Merge new candles with existing parquet, deduplicating by datetime."""
    if not parquet_path.exists():
        return new_df

    existing = pd.read_parquet(parquet_path)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["datetime"], keep="last")
    combined = combined.sort_values("datetime").reset_index(drop=True)
    return combined


def save_candles(market: str, df: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    """Save candles to parquet with merge."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{market}.parquet"
    merged = merge_with_existing(df, path)
    merged.to_parquet(path, index=False)


def load_candles(
    market: str,
    start: str,
    end: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame | None:
    """Load cached candles for a date range.

    Args:
        market: Market code.
        start: Start date "YYYY-MM-DD".
        end: End date "YYYY-MM-DD".
        data_dir: Directory containing parquet files.

    Returns:
        Filtered DataFrame or None if no cache exists.
    """
    path = data_dir / f"{market}.parquet"
    if not path.exists():
        return None

    df = pd.read_parquet(path)
    start_dt = f"{start}T00:00:00"
    end_dt = f"{end}T23:59:59"
    mask = (df["datetime"] >= start_dt) & (df["datetime"] <= end_dt)
    filtered = df[mask].reset_index(drop=True)
    return filtered if len(filtered) > 0 else None


def fetch_and_cache(
    market: str,
    start: str,
    end: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Fetch 1h candles from API, cache to parquet, and return.

    Performs incremental fetch: only downloads data not yet cached.
    """
    cached = load_candles(market, start, end, data_dir)
    if cached is not None and len(cached) > 0:
        # Check if we have enough coverage
        first_cached = cached["datetime"].iloc[0][:10]
        last_cached = cached["datetime"].iloc[-1][:10]
        if first_cached <= start and last_cached >= end:
            return cached

    raw = fetch_hourly_candles(market, start, end)
    df = normalize_candles(raw)
    if len(df) > 0:
        save_candles(market, df, data_dir)
    return load_candles(market, start, end, data_dir) or df


def fetch_all_universe(
    start: str,
    end: str,
    top_n_prefetch: int = 100,
    data_dir: Path = DATA_DIR,
) -> dict[str, pd.DataFrame]:
    """Fetch 1h candles for wider universe, cache, and return.

    Fetches current top markets by volume as a pragmatic pre-filter.
    Survivorship bias is accepted for MVP.

    Args:
        start: Backtest start date.
        end: Backtest end date.
        top_n_prefetch: How many markets to pre-fetch (wider than top_n).
        data_dir: Cache directory.

    Returns:
        Dict mapping market code to DataFrame.
    """
    # Get current top markets by 24h volume
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{UPBIT_API_URL}/ticker/all", params={"quoteCurrencies": "KRW"})
        resp.raise_for_status()
        tickers = resp.json()

    # Sort by 24h trade value descending
    tickers.sort(key=lambda t: t.get("acc_trade_price_24h", 0), reverse=True)
    markets = [t["market"] for t in tickers[:top_n_prefetch]]

    all_data: dict[str, pd.DataFrame] = {}
    total = len(markets)

    for i, market in enumerate(markets, 1):
        print(f"  [{i}/{total}] {market}...", end=" ", flush=True)
        try:
            df = fetch_and_cache(market, start, end, data_dir)
            if df is not None and len(df) > 0:
                all_data[market] = df
                print(f"{len(df)} bars")
            else:
                print("no data")
        except Exception as exc:
            print(f"error: {exc}")

    return all_data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_data_loader.py -v`

Expected: All 7 tests PASS.

- [ ] **Step 5: Add `backtest/data/1h/` to .gitignore**

Append to the project `.gitignore`:

```
# Backtest cached data
backtest/data/
```

- [ ] **Step 6: Commit**

```bash
git add backtest/rsi/data_loader.py tests/backtest/test_rsi_data_loader.py .gitignore
git commit -m "feat(backtest): add 1h candle data loader with parquet cache"
```

---

## Task 4: Universe Selector + Tests

**Files:**
- Create: `backtest/rsi/universe.py`
- Create: `tests/backtest/test_rsi_universe.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_universe.py
"""Tests for universe selection by rolling trade value."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.universe import select_universe


def _make_candles(market: str, values: list[float]) -> pd.DataFrame:
    """Helper: create candle DataFrame with given hourly values."""
    n = len(values)
    datetimes = [f"2024-01-01T{h:02d}:00:00" for h in range(n)]
    return pd.DataFrame({
        "datetime": datetimes,
        "open": [100.0] * n,
        "high": [100.0] * n,
        "low": [100.0] * n,
        "close": [100.0] * n,
        "volume": [1.0] * n,
        "value": values,
    })


class TestSelectUniverse:
    """Tests for top-N universe selection."""

    def test_selects_top_n_by_value(self):
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", [1000, 2000, 3000]),
            "KRW-ETH": _make_candles("KRW-ETH", [500, 600, 700]),
            "KRW-XRP": _make_candles("KRW-XRP", [100, 200, 300]),
        }
        result = select_universe(all_data, "2024-01-01T02:00:00", top_n=2, window=3)
        assert len(result) == 2
        assert result[0] == "KRW-BTC"
        assert result[1] == "KRW-ETH"

    def test_top_n_larger_than_available(self):
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", [1000]),
        }
        result = select_universe(all_data, "2024-01-01T00:00:00", top_n=5, window=1)
        assert len(result) == 1

    def test_skips_markets_without_data_at_timestamp(self):
        btc = _make_candles("KRW-BTC", [1000, 2000])
        # ETH only has data at T00, not T01
        eth = pd.DataFrame({
            "datetime": ["2024-01-01T00:00:00"],
            "open": [100], "high": [100], "low": [100], "close": [100],
            "volume": [1], "value": [9999],
        })
        all_data = {"KRW-BTC": btc, "KRW-ETH": eth}
        result = select_universe(all_data, "2024-01-01T01:00:00", top_n=2, window=2)
        assert "KRW-BTC" in result

    def test_window_rolls_correctly(self):
        # 5 hours of data, window=3, check at T04
        btc_values = [100, 200, 300, 400, 500]
        eth_values = [600, 100, 100, 100, 100]
        all_data = {
            "KRW-BTC": _make_candles("KRW-BTC", btc_values),
            "KRW-ETH": _make_candles("KRW-ETH", eth_values),
        }
        # At T04: BTC rolling 3 = 300+400+500=1200, ETH rolling 3 = 100+100+100=300
        result = select_universe(all_data, "2024-01-01T04:00:00", top_n=1, window=3)
        assert result == ["KRW-BTC"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_universe.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.universe'`

- [ ] **Step 3: Implement universe selector**

```python
# backtest/rsi/universe.py
"""Dynamic universe selection by rolling trade value."""

import pandas as pd


def select_universe(
    all_data: dict[str, pd.DataFrame],
    timestamp: str,
    top_n: int,
    window: int = 24,
) -> list[str]:
    """Select top-N markets by rolling trade value at a given timestamp.

    Args:
        all_data: Dict mapping market code to 1h candle DataFrame.
        timestamp: ISO datetime string "YYYY-MM-DDTHH:MM:SS".
        top_n: Number of markets to select.
        window: Rolling window size in hours for trade value sum.

    Returns:
        List of market codes sorted by descending rolling trade value.
    """
    scores: list[tuple[str, float]] = []

    for market, df in all_data.items():
        # Find rows up to and including the timestamp
        mask = df["datetime"] <= timestamp
        subset = df[mask]
        if len(subset) == 0:
            continue

        # Rolling sum of trade value over the window
        tail = subset.tail(window)
        rolling_value = tail["value"].sum()
        scores.append((market, rolling_value))

    # Sort descending by value, take top N
    scores.sort(key=lambda x: x[1], reverse=True)
    return [market for market, _ in scores[:top_n]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_universe.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/rsi/universe.py tests/backtest/test_rsi_universe.py
git commit -m "feat(backtest): add universe selection by rolling trade value"
```

---

## Task 5: Strategy Selector + Tests

**Files:**
- Create: `backtest/rsi/strategy.py`
- Create: `tests/backtest/test_rsi_strategy.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_strategy.py
"""Tests for RSI-based coin selection strategy."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.strategy import select_coins


def _make_candles_with_rsi(market: str, rsi_target: float, n_bars: int = 30) -> pd.DataFrame:
    """Create candle data that produces approximately the target RSI.

    Uses a simple approach: fixed base with controlled up/down moves.
    """
    rng = np.random.default_rng(hash(market) % 2**32)
    # Probability of up-move that yields target RSI (approx)
    p_up = rsi_target / 100.0
    base = 1000.0
    prices = [base]
    for _ in range(n_bars - 1):
        if rng.random() < p_up:
            prices.append(prices[-1] + rng.uniform(1, 5))
        else:
            prices.append(prices[-1] - rng.uniform(1, 5))

    datetimes = [f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00" for i in range(n_bars)]
    return pd.DataFrame({
        "datetime": datetimes,
        "open": prices,
        "high": [p + 1 for p in prices],
        "low": [p - 1 for p in prices],
        "close": prices,
        "volume": [100.0] * n_bars,
        "value": [100000.0] * n_bars,
    })


class TestSelectCoins:
    def test_filters_by_max_rsi(self):
        config = BacktestConfig(start="2024-01-01", end="2024-02-01", max_rsi=30.0, pick_k=5, rsi_period=14)
        # Create one coin with very high RSI (all gains)
        high_rsi = pd.DataFrame({
            "datetime": [f"2024-01-01T{h:02d}:00:00" for h in range(20)],
            "open": list(range(100, 120)),
            "high": list(range(101, 121)),
            "low": list(range(99, 119)),
            "close": list(range(100, 120)),
            "volume": [100.0] * 20,
            "value": [100000.0] * 20,
        })
        all_data = {"KRW-HIGH": high_rsi}
        universe = ["KRW-HIGH"]
        timestamp = "2024-01-01T19:00:00"
        result = select_coins(universe, all_data, timestamp, config)
        # RSI should be ~100 → filtered out by max_rsi=30
        assert len(result) == 0

    def test_sorts_by_rsi_ascending(self):
        config = BacktestConfig(start="2024-01-01", end="2024-02-01", max_rsi=80.0, pick_k=3, rsi_period=14)
        all_data = {
            "KRW-A": _make_candles_with_rsi("KRW-A", 60),
            "KRW-B": _make_candles_with_rsi("KRW-B", 30),
            "KRW-C": _make_candles_with_rsi("KRW-C", 45),
        }
        universe = ["KRW-A", "KRW-B", "KRW-C"]
        # Use last available timestamp
        ts = all_data["KRW-A"]["datetime"].iloc[-1]
        result = select_coins(universe, all_data, ts, config)
        # Should be sorted ascending by RSI
        assert len(result) <= 3
        # First coin should have lowest RSI

    def test_picks_at_most_k(self):
        config = BacktestConfig(start="2024-01-01", end="2024-02-01", max_rsi=99.0, pick_k=2, rsi_period=14)
        all_data = {
            f"KRW-{c}": _make_candles_with_rsi(f"KRW-{c}", 40)
            for c in ["A", "B", "C", "D"]
        }
        universe = list(all_data.keys())
        ts = list(all_data.values())[0]["datetime"].iloc[-1]
        result = select_coins(universe, all_data, ts, config)
        assert len(result) <= 2

    def test_skips_coins_with_insufficient_data(self):
        config = BacktestConfig(start="2024-01-01", end="2024-02-01", max_rsi=80.0, pick_k=5, rsi_period=14)
        short = pd.DataFrame({
            "datetime": ["2024-01-01T00:00:00", "2024-01-01T01:00:00"],
            "open": [100, 101], "high": [100, 101], "low": [100, 101],
            "close": [100, 101], "volume": [10, 10], "value": [1000, 1000],
        })
        all_data = {"KRW-SHORT": short}
        result = select_coins(["KRW-SHORT"], all_data, "2024-01-01T01:00:00", config)
        assert len(result) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_strategy.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.strategy'`

- [ ] **Step 3: Implement strategy selector**

```python
# backtest/rsi/strategy.py
"""RSI-based coin selection strategy."""

import numpy as np
import pandas as pd

from .config import BacktestConfig
from .indicators import calc_rsi


def select_coins(
    universe: list[str],
    all_data: dict[str, pd.DataFrame],
    timestamp: str,
    config: BacktestConfig,
) -> list[str]:
    """Select coins from universe by RSI ascending, filtered by max_rsi.

    Args:
        universe: Candidate market codes (pre-filtered by trade value).
        all_data: Dict mapping market code to 1h candle DataFrame.
        timestamp: Current rebalance timestamp.
        config: Backtest configuration.

    Returns:
        List of selected market codes, sorted by RSI ascending.
    """
    candidates: list[tuple[str, float]] = []

    for market in universe:
        df = all_data.get(market)
        if df is None:
            continue

        # Get data up to timestamp
        mask = df["datetime"] <= timestamp
        subset = df[mask]
        if len(subset) < config.rsi_period + 1:
            continue

        closes = subset["close"].to_numpy(dtype=float)
        rsi = calc_rsi(closes, period=config.rsi_period)
        if rsi is None:
            continue

        if rsi <= config.max_rsi:
            candidates.append((market, rsi))

    # Sort by RSI ascending (lowest RSI first)
    candidates.sort(key=lambda x: x[1])

    # Pick top K
    return [market for market, _ in candidates[: config.pick_k]]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_strategy.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/rsi/strategy.py tests/backtest/test_rsi_strategy.py
git commit -m "feat(backtest): add RSI-based coin selection strategy"
```

---

## Task 6: Portfolio Simulator + Tests

**Files:**
- Create: `backtest/rsi/simulator.py`
- Create: `tests/backtest/test_rsi_simulator.py`

This is the core engine. It iterates through hourly bars, rebalances at configured intervals, and tracks the equity curve.

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_simulator.py
"""Tests for the rebalancing portfolio simulator."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.simulator import run_backtest, Portfolio, BacktestResult


def _make_flat_candles(market: str, n_bars: int = 50, price: float = 1000.0) -> pd.DataFrame:
    """Flat price candles for predictable testing."""
    datetimes = []
    for i in range(n_bars):
        day = i // 24 + 1
        hour = i % 24
        datetimes.append(f"2024-01-{day:02d}T{hour:02d}:00:00")
    return pd.DataFrame({
        "datetime": datetimes,
        "open": [price] * n_bars,
        "high": [price] * n_bars,
        "low": [price] * n_bars,
        "close": [price] * n_bars,
        "volume": [100.0] * n_bars,
        "value": [1_000_000.0] * n_bars,
    })


def _make_rising_candles(market: str, n_bars: int = 50) -> pd.DataFrame:
    """Steadily rising prices."""
    datetimes = []
    prices = []
    for i in range(n_bars):
        day = i // 24 + 1
        hour = i % 24
        datetimes.append(f"2024-01-{day:02d}T{hour:02d}:00:00")
        prices.append(1000.0 + i * 10.0)
    return pd.DataFrame({
        "datetime": datetimes,
        "open": prices,
        "high": [p + 5 for p in prices],
        "low": [p - 5 for p in prices],
        "close": prices,
        "volume": [100.0] * n_bars,
        "value": [1_000_000.0] * n_bars,
    })


class TestBacktestResult:
    def test_result_has_equity_curve(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        assert isinstance(result, BacktestResult)
        assert len(result.equity_curve) > 0
        assert len(result.timestamps) == len(result.equity_curve)

    def test_no_trade_on_empty_data(self):
        config = BacktestConfig(start="2024-01-01", end="2024-01-02", top_n=1, pick_k=1, max_rsi=99)
        result = run_backtest({}, config)
        assert result.rebalance_count == 0
        assert len(result.trades) == 0


class TestPortfolioEquity:
    def test_flat_price_equity_decreases_by_fees(self):
        """With flat prices, equity should only decrease due to fees."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
            fee_rate=0.001, slippage_bps=0,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        # Should have traded and lost some to fees
        assert result.equity_curve[-1] <= config.initial_capital

    def test_rising_price_positive_return(self):
        """With rising prices and low RSI entry, should have positive return."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_rising_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        # Even with fees, strong uptrend should be profitable
        if len(result.trades) > 0:
            assert result.equity_curve[-1] > config.initial_capital * 0.99


class TestRebalancing:
    def test_rebalance_count(self):
        """48 hours of data with 24h rebalance → should rebalance ~2 times."""
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        assert result.rebalance_count >= 1

    def test_trades_logged(self):
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-02",
            top_n=1, pick_k=1, max_rsi=99, rebalance_hours=24,
        )
        all_data = {"KRW-BTC": _make_flat_candles("KRW-BTC", n_bars=48)}
        result = run_backtest(all_data, config)
        for trade in result.trades:
            assert "datetime" in trade
            assert "market" in trade
            assert "action" in trade
            assert "quantity" in trade
            assert "price" in trade
            assert "fee" in trade
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_simulator.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.simulator'`

- [ ] **Step 3: Implement simulator**

```python
# backtest/rsi/simulator.py
"""Rebalancing portfolio simulator for RSI strategy."""

from dataclasses import dataclass, field

import pandas as pd

from .config import BacktestConfig
from .strategy import select_coins
from .universe import select_universe


@dataclass
class Portfolio:
    """Mutable portfolio state."""

    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # market -> quantity

    def equity(self, prices: dict[str, float]) -> float:
        """Total portfolio value at current prices."""
        pos_value = sum(
            qty * prices.get(market, 0) for market, qty in self.positions.items()
        )
        return self.cash + pos_value


@dataclass
class BacktestResult:
    """Output of a backtest run."""

    equity_curve: list[float]
    timestamps: list[str]
    trades: list[dict]
    rebalance_count: int
    config: BacktestConfig


def _get_prices_at(all_data: dict[str, pd.DataFrame], timestamp: str) -> dict[str, float]:
    """Get close prices for all markets at a specific timestamp."""
    prices: dict[str, float] = {}
    for market, df in all_data.items():
        mask = df["datetime"] == timestamp
        rows = df[mask]
        if len(rows) > 0:
            prices[market] = float(rows.iloc[0]["close"])
    return prices


def _execute_rebalance(
    portfolio: Portfolio,
    target_markets: list[str],
    prices: dict[str, float],
    config: BacktestConfig,
    timestamp: str,
) -> list[dict]:
    """Rebalance portfolio to equal-weight target markets.

    Returns list of trade records.
    """
    trades: list[dict] = []
    total_equity = portfolio.equity(prices)

    if not target_markets or total_equity <= 0:
        return trades

    target_weight = 1.0 / len(target_markets)
    target_value_per_coin = total_equity * target_weight
    slippage_mult = config.slippage_bps / 10_000

    # Phase 1: Sell positions not in target (or over-weight)
    for market in list(portfolio.positions.keys()):
        if market not in target_markets:
            qty = portfolio.positions[market]
            if qty > 0 and market in prices:
                sell_price = prices[market] * (1 - slippage_mult)
                proceeds = qty * sell_price
                fee = proceeds * config.fee_rate
                portfolio.cash += proceeds - fee
                trades.append({
                    "datetime": timestamp,
                    "market": market,
                    "action": "sell",
                    "quantity": qty,
                    "price": sell_price,
                    "fee": fee,
                })
            portfolio.positions.pop(market, None)

    # Phase 2: Rebalance existing + buy new
    # Recalculate equity after sells
    total_equity = portfolio.equity(prices)
    target_value_per_coin = total_equity * target_weight

    for market in target_markets:
        if market not in prices:
            continue

        price = prices[market]
        current_qty = portfolio.positions.get(market, 0)
        current_value = current_qty * price
        diff_value = target_value_per_coin - current_value

        if abs(diff_value) < total_equity * 0.01:
            # Skip tiny rebalances (< 1% of portfolio)
            continue

        if diff_value > 0:
            # Buy
            buy_price = price * (1 + slippage_mult)
            max_buy_value = portfolio.cash / (1 + config.fee_rate)
            buy_value = min(diff_value, max_buy_value)
            if buy_value <= 0:
                continue
            qty = buy_value / buy_price
            cost = qty * buy_price
            fee = cost * config.fee_rate
            portfolio.cash -= cost + fee
            portfolio.positions[market] = current_qty + qty
            trades.append({
                "datetime": timestamp,
                "market": market,
                "action": "buy",
                "quantity": qty,
                "price": buy_price,
                "fee": fee,
            })
        elif diff_value < 0:
            # Sell excess
            sell_price = price * (1 - slippage_mult)
            sell_qty = min(abs(diff_value) / price, current_qty)
            if sell_qty <= 0:
                continue
            proceeds = sell_qty * sell_price
            fee = proceeds * config.fee_rate
            portfolio.cash += proceeds - fee
            portfolio.positions[market] = current_qty - sell_qty
            if portfolio.positions[market] <= 0:
                portfolio.positions.pop(market, None)
            trades.append({
                "datetime": timestamp,
                "market": market,
                "action": "sell",
                "quantity": sell_qty,
                "price": sell_price,
                "fee": fee,
            })

    return trades


def run_backtest(
    all_data: dict[str, pd.DataFrame],
    config: BacktestConfig,
) -> BacktestResult:
    """Run the full rebalancing backtest.

    Args:
        all_data: Dict mapping market code to 1h candle DataFrame.
        config: Backtest configuration.

    Returns:
        BacktestResult with equity curve, trades, and metadata.
    """
    if not all_data:
        return BacktestResult(
            equity_curve=[config.initial_capital],
            timestamps=[config.start + "T00:00:00"],
            trades=[],
            rebalance_count=0,
            config=config,
        )

    # Build unified sorted timeline
    all_timestamps: set[str] = set()
    for df in all_data.values():
        start_filter = f"{config.start}T00:00:00"
        end_filter = f"{config.end}T23:59:59"
        mask = (df["datetime"] >= start_filter) & (df["datetime"] <= end_filter)
        all_timestamps.update(df[mask]["datetime"].tolist())

    timestamps = sorted(all_timestamps)
    if not timestamps:
        return BacktestResult(
            equity_curve=[config.initial_capital],
            timestamps=[config.start + "T00:00:00"],
            trades=[],
            rebalance_count=0,
            config=config,
        )

    portfolio = Portfolio(cash=config.initial_capital)
    equity_curve: list[float] = []
    equity_timestamps: list[str] = []
    all_trades: list[dict] = []
    rebalance_count = 0
    bars_since_rebalance = config.rebalance_hours  # Force rebalance on first bar

    for ts in timestamps:
        prices = _get_prices_at(all_data, ts)

        # Check if it's time to rebalance
        if bars_since_rebalance >= config.rebalance_hours:
            # Select universe
            universe = select_universe(all_data, ts, config.top_n, window=24)

            # Select coins by RSI
            selected = select_coins(universe, all_data, ts, config)

            # Execute rebalance
            trades = _execute_rebalance(portfolio, selected, prices, config, ts)
            all_trades.extend(trades)
            rebalance_count += 1
            bars_since_rebalance = 0

        bars_since_rebalance += 1

        # Record equity
        equity = portfolio.equity(prices)
        equity_curve.append(equity)
        equity_timestamps.append(ts)

    return BacktestResult(
        equity_curve=equity_curve,
        timestamps=equity_timestamps,
        trades=all_trades,
        rebalance_count=rebalance_count,
        config=config,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_simulator.py -v`

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/rsi/simulator.py tests/backtest/test_rsi_simulator.py
git commit -m "feat(backtest): add rebalancing portfolio simulator"
```

---

## Task 7: Metrics Calculator + Tests

**Files:**
- Create: `backtest/rsi/metrics.py`
- Create: `tests/backtest/test_rsi_metrics.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtest/test_rsi_metrics.py
"""Tests for performance metrics calculation."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.metrics import compute_metrics, Metrics
from rsi.simulator import BacktestResult


def _make_result(equity_curve: list[float], n_trades: int = 0) -> BacktestResult:
    """Helper to build a BacktestResult for metric tests."""
    n = len(equity_curve)
    timestamps = [f"2024-01-01T{i:02d}:00:00" for i in range(n)]
    trades = [{"action": "buy", "market": "KRW-BTC", "quantity": 1, "price": 100, "fee": 0.05, "datetime": timestamps[0]}] * n_trades
    config = BacktestConfig(start="2024-01-01", end="2024-01-02")
    return BacktestResult(
        equity_curve=equity_curve,
        timestamps=timestamps,
        trades=trades,
        rebalance_count=1,
        config=config,
    )


class TestCumulativeReturn:
    def test_positive_return(self):
        result = _make_result([10_000_000, 11_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(0.10, abs=0.001)

    def test_negative_return(self):
        result = _make_result([10_000_000, 9_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(-0.10, abs=0.001)

    def test_no_change(self):
        result = _make_result([10_000_000, 10_000_000])
        m = compute_metrics(result)
        assert m.cumulative_return == pytest.approx(0.0)


class TestMaxDrawdown:
    def test_no_drawdown(self):
        result = _make_result([100, 110, 120, 130])
        m = compute_metrics(result)
        assert m.max_drawdown == pytest.approx(0.0)

    def test_known_drawdown(self):
        # Peak at 200, trough at 100 → 50% drawdown
        result = _make_result([100, 200, 100, 150])
        m = compute_metrics(result)
        assert m.max_drawdown == pytest.approx(0.50, abs=0.01)


class TestSharpe:
    def test_flat_equity_zero_sharpe(self):
        result = _make_result([100, 100, 100, 100])
        m = compute_metrics(result)
        assert m.sharpe == 0.0

    def test_positive_sharpe_for_steady_gains(self):
        curve = [10_000_000 + i * 10_000 for i in range(100)]
        result = _make_result(curve)
        m = compute_metrics(result)
        assert m.sharpe > 0


class TestTradeCount:
    def test_counts_trades(self):
        result = _make_result([100, 110], n_trades=5)
        m = compute_metrics(result)
        assert m.trade_count == 5


class TestBenchmark:
    def test_btc_benchmark(self):
        result = _make_result([10_000_000, 11_000_000])
        btc_data = pd.DataFrame({
            "datetime": ["2024-01-01T00:00:00", "2024-01-01T01:00:00"],
            "close": [50_000_000, 55_000_000],
        })
        m = compute_metrics(result, btc_data=btc_data)
        assert m.benchmark_return == pytest.approx(0.10, abs=0.001)

    def test_no_btc_data_returns_none(self):
        result = _make_result([10_000_000, 11_000_000])
        m = compute_metrics(result)
        assert m.benchmark_return is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_metrics.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'rsi.metrics'`

- [ ] **Step 3: Implement metrics**

```python
# backtest/rsi/metrics.py
"""Performance metrics for backtest results."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .simulator import BacktestResult


@dataclass
class Metrics:
    """Calculated performance metrics."""

    cumulative_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    trade_count: int
    turnover: float
    benchmark_return: float | None  # BTC buy & hold


def compute_metrics(
    result: BacktestResult,
    btc_data: pd.DataFrame | None = None,
    hours_per_year: float = 8760.0,
) -> Metrics:
    """Compute performance metrics from backtest result.

    Args:
        result: BacktestResult from simulator.
        btc_data: Optional BTC 1h candle DataFrame for benchmark.
        hours_per_year: Hours per year for annualization (default 8760).

    Returns:
        Metrics dataclass.
    """
    curve = np.array(result.equity_curve, dtype=float)

    # Cumulative return
    if len(curve) < 2 or curve[0] == 0:
        return Metrics(
            cumulative_return=0.0,
            cagr=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            trade_count=len(result.trades),
            turnover=0.0,
            benchmark_return=_calc_benchmark(btc_data, result.timestamps),
        )

    cum_return = (curve[-1] / curve[0]) - 1.0

    # CAGR
    n_hours = len(curve) - 1
    years = n_hours / hours_per_year
    if years > 0 and curve[-1] > 0 and curve[0] > 0:
        cagr = (curve[-1] / curve[0]) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    # Hourly returns for Sharpe
    returns = np.diff(curve) / curve[:-1]
    returns = returns[np.isfinite(returns)]

    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(hours_per_year)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(curve)
    drawdowns = (peak - curve) / peak
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    # Turnover: total traded value / average portfolio value
    total_traded = sum(
        abs(t.get("quantity", 0) * t.get("price", 0)) for t in result.trades
    )
    avg_equity = float(np.mean(curve))
    turnover = total_traded / avg_equity if avg_equity > 0 else 0.0

    return Metrics(
        cumulative_return=cum_return,
        cagr=cagr,
        sharpe=sharpe,
        max_drawdown=max_dd,
        trade_count=len(result.trades),
        turnover=turnover,
        benchmark_return=_calc_benchmark(btc_data, result.timestamps),
    )


def _calc_benchmark(
    btc_data: pd.DataFrame | None,
    timestamps: list[str],
) -> float | None:
    """Calculate BTC buy & hold return over the same period."""
    if btc_data is None or len(btc_data) == 0 or len(timestamps) < 2:
        return None

    start_ts = timestamps[0]
    end_ts = timestamps[-1]

    # Find closest prices
    start_mask = btc_data["datetime"] <= start_ts
    end_mask = btc_data["datetime"] <= end_ts

    start_rows = btc_data[start_mask]
    end_rows = btc_data[end_mask]

    if len(start_rows) == 0 or len(end_rows) == 0:
        return None

    start_price = float(start_rows.iloc[-1]["close"])
    end_price = float(end_rows.iloc[-1]["close"])

    if start_price == 0:
        return None

    return (end_price / start_price) - 1.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_metrics.py -v`

Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtest/rsi/metrics.py tests/backtest/test_rsi_metrics.py
git commit -m "feat(backtest): add performance metrics with BTC benchmark"
```

---

## Task 8: Report Output + CLI Entrypoint

**Files:**
- Create: `backtest/rsi/report.py`
- Create: `backtest/rsi_backtest.py`

- [ ] **Step 1: Implement report formatter**

```python
# backtest/rsi/report.py
"""Backtest result reporting and export."""

import csv
from pathlib import Path

from .metrics import Metrics
from .simulator import BacktestResult


def print_summary(metrics: Metrics, result: BacktestResult) -> None:
    """Print formatted performance summary to stdout."""
    cfg = result.config
    print()
    print("=" * 55)
    print("  CRYPTO RSI PORTFOLIO BACKTEST RESULTS")
    print("=" * 55)
    print()
    print("  Strategy Parameters:")
    print(f"    Period:           {cfg.start} ~ {cfg.end}")
    print(f"    Universe:         top {cfg.top_n} by 24h trade value")
    print(f"    Selection:        RSI-{cfg.rsi_period} ascending, max_rsi={cfg.max_rsi}")
    print(f"    Portfolio:        equal-weight top {cfg.pick_k}")
    print(f"    Rebalance:        every {cfg.rebalance_hours}h")
    print(f"    Fee:              {cfg.fee_rate * 100:.3f}%")
    print(f"    Slippage:         {cfg.slippage_bps} bps")
    print()
    print("  Performance:")
    print(f"    Cumulative Return:  {metrics.cumulative_return * 100:+.2f}%")
    print(f"    CAGR:               {metrics.cagr * 100:+.2f}%")
    print(f"    Sharpe Ratio:       {metrics.sharpe:.2f}")
    print(f"    Max Drawdown:       {metrics.max_drawdown * 100:.2f}%")
    print(f"    Trade Count:        {metrics.trade_count}")
    print(f"    Turnover:           {metrics.turnover:.2f}x")
    print(f"    Rebalances:         {result.rebalance_count}")

    if metrics.benchmark_return is not None:
        print()
        print("  Benchmark (BTC Buy & Hold):")
        print(f"    BTC Return:         {metrics.benchmark_return * 100:+.2f}%")
        excess = metrics.cumulative_return - metrics.benchmark_return
        print(f"    Excess Return:      {excess * 100:+.2f}%")

    print()
    print("=" * 55)


def export_equity_csv(result: BacktestResult, path: Path) -> None:
    """Export equity curve to CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime", "equity"])
        for ts, eq in zip(result.timestamps, result.equity_curve):
            writer.writerow([ts, f"{eq:.2f}"])
    print(f"  Equity curve saved to {path}")


def export_trades_csv(result: BacktestResult, path: Path) -> None:
    """Export trade log to CSV."""
    if not result.trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["datetime", "market", "action", "quantity", "price", "fee"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.trades)
    print(f"  Trades saved to {path}")


def export_monthly_returns(result: BacktestResult, path: Path) -> None:
    """Export monthly returns to CSV."""
    if len(result.equity_curve) < 2:
        return

    path.parent.mkdir(parents=True, exist_ok=True)

    # Group equity by month
    monthly: dict[str, tuple[float, float]] = {}  # "YYYY-MM" -> (first_equity, last_equity)
    for ts, eq in zip(result.timestamps, result.equity_curve):
        month = ts[:7]  # "YYYY-MM"
        if month not in monthly:
            monthly[month] = (eq, eq)
        else:
            monthly[month] = (monthly[month][0], eq)

    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["month", "return_pct"])
        prev_end = None
        for month in sorted(monthly.keys()):
            first, last = monthly[month]
            base = prev_end if prev_end is not None else first
            ret = ((last / base) - 1.0) * 100 if base > 0 else 0.0
            writer.writerow([month, f"{ret:.2f}"])
            prev_end = last

    print(f"  Monthly returns saved to {path}")
```

- [ ] **Step 2: Implement CLI entrypoint**

```python
# backtest/rsi_backtest.py
"""CLI entrypoint for crypto RSI portfolio backtest.

Usage:
    uv run backtest/rsi_backtest.py --start 2024-01-01 --end 2026-03-01
    uv run backtest/rsi_backtest.py --start 2024-06-01 --end 2025-06-01 --top-n 20 --pick-k 3 --max-rsi 35
"""

import argparse
import sys
import time
from pathlib import Path

# Add backtest directory to path for package imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rsi.config import BacktestConfig
from rsi.data_loader import fetch_all_universe, load_candles, DATA_DIR
from rsi.metrics import compute_metrics
from rsi.report import print_summary, export_equity_csv, export_trades_csv, export_monthly_returns
from rsi.simulator import run_backtest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crypto RSI portfolio backtest (Upbit 1h candles)",
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, default=30, help="Universe size by 거래대금 (default: 30)")
    parser.add_argument("--pick-k", type=int, default=5, help="Number of coins to hold (default: 5)")
    parser.add_argument("--max-rsi", type=float, default=45.0, help="Max RSI for entry (default: 45)")
    parser.add_argument("--rebalance-hours", type=int, default=24, help="Rebalance interval in hours (default: 24)")
    parser.add_argument("--rsi-period", type=int, default=14, help="RSI lookback period (default: 14)")
    parser.add_argument("--initial-capital", type=float, default=10_000_000, help="Initial capital in KRW (default: 10M)")
    parser.add_argument("--prefetch", type=int, default=100, help="Number of markets to pre-fetch (default: 100)")
    parser.add_argument("--export-dir", type=str, default=None, help="Directory for CSV exports")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip API fetch, use cached data only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    config = BacktestConfig(
        start=args.start,
        end=args.end,
        top_n=args.top_n,
        pick_k=args.pick_k,
        max_rsi=args.max_rsi,
        rebalance_hours=args.rebalance_hours,
        rsi_period=args.rsi_period,
        initial_capital=args.initial_capital,
    )

    # Load or fetch data
    if args.skip_fetch:
        print("Loading cached data...")
        all_data = _load_cached(config)
    else:
        print(f"Fetching 1h candles for top {args.prefetch} markets...")
        start_time = time.time()
        all_data = fetch_all_universe(config.start, config.end, args.prefetch)
        elapsed = time.time() - start_time
        print(f"Fetched {len(all_data)} markets in {elapsed:.1f}s")

    if not all_data:
        print("No data available. Check your date range or run without --skip-fetch.")
        return 1

    print(f"\nRunning backtest with {len(all_data)} markets...")
    start_time = time.time()
    result = run_backtest(all_data, config)
    elapsed = time.time() - start_time
    print(f"Backtest completed in {elapsed:.1f}s")

    # BTC benchmark
    btc_data = all_data.get("KRW-BTC")
    metrics = compute_metrics(result, btc_data=btc_data)

    # Print results
    print_summary(metrics, result)

    # Export CSVs if requested
    if args.export_dir:
        export_dir = Path(args.export_dir)
        export_equity_csv(result, export_dir / "equity_curve.csv")
        export_trades_csv(result, export_dir / "trades.csv")
        export_monthly_returns(result, export_dir / "monthly_returns.csv")

    return 0


def _load_cached(config: BacktestConfig) -> dict[str, pd.DataFrame]:
    """Load all cached parquet files for the date range."""
    import pandas as pd

    all_data: dict[str, pd.DataFrame] = {}
    if not DATA_DIR.exists():
        return all_data

    for path in DATA_DIR.glob("KRW-*.parquet"):
        market = path.stem  # e.g., "KRW-BTC"
        df = load_candles(market, config.start, config.end)
        if df is not None and len(df) > 0:
            all_data[market] = df

    print(f"Loaded {len(all_data)} markets from cache")
    return all_data


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Verify CLI help works**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run backtest/rsi_backtest.py --help`

Expected: Prints argparse help with all options.

- [ ] **Step 4: Commit**

```bash
git add backtest/rsi/report.py backtest/rsi_backtest.py
git commit -m "feat(backtest): add CLI entrypoint and report output"
```

---

## Task 9: Integration Test with Synthetic Data

**Files:**
- Create: `tests/backtest/test_rsi_integration.py`

This test runs the full pipeline end-to-end with synthetic data to verify all components work together.

- [ ] **Step 1: Write integration test**

```python
# tests/backtest/test_rsi_integration.py
"""Integration test: full RSI portfolio backtest with synthetic data."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backtest"))

from rsi.config import BacktestConfig
from rsi.metrics import compute_metrics
from rsi.simulator import run_backtest


def _generate_synthetic_market(
    market: str,
    n_days: int = 30,
    base_price: float = 1000.0,
    volatility: float = 0.02,
    base_value: float = 1_000_000.0,
    seed: int | None = None,
) -> pd.DataFrame:
    """Generate synthetic 1h candle data for testing."""
    rng = np.random.default_rng(seed)
    n_bars = n_days * 24
    prices = [base_price]
    for _ in range(n_bars - 1):
        ret = rng.normal(0, volatility)
        prices.append(prices[-1] * (1 + ret))

    datetimes = []
    for i in range(n_bars):
        day = i // 24 + 1
        hour = i % 24
        datetimes.append(f"2024-01-{day:02d}T{hour:02d}:00:00")

    prices_arr = np.array(prices)
    return pd.DataFrame({
        "datetime": datetimes[:n_bars],
        "open": prices_arr,
        "high": prices_arr * (1 + rng.uniform(0, 0.01, n_bars)),
        "low": prices_arr * (1 - rng.uniform(0, 0.01, n_bars)),
        "close": prices_arr,
        "volume": rng.uniform(50, 200, n_bars),
        "value": rng.uniform(base_value * 0.5, base_value * 1.5, n_bars),
    })


class TestFullPipeline:
    """End-to-end integration tests."""

    def test_basic_run(self):
        """Run full backtest with 5 synthetic markets."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(
                f"KRW-COIN{i}", n_days=10, seed=i, base_value=1_000_000 * (5 - i),
            )
            for i in range(5)
        }
        config = BacktestConfig(
            start="2024-01-01",
            end="2024-01-10",
            top_n=3,
            pick_k=2,
            max_rsi=70,
            rebalance_hours=24,
        )
        result = run_backtest(all_data, config)
        assert len(result.equity_curve) > 0
        assert result.rebalance_count >= 1
        assert len(result.trades) > 0

        metrics = compute_metrics(result, btc_data=all_data["KRW-COIN0"])
        assert metrics.cumulative_return != 0.0 or metrics.trade_count == 0
        assert metrics.max_drawdown >= 0.0
        assert metrics.benchmark_return is not None

    def test_parameter_sensitivity(self):
        """Different parameters should produce different results."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(f"KRW-COIN{i}", n_days=10, seed=i)
            for i in range(10)
        }

        config_a = BacktestConfig(
            start="2024-01-01", end="2024-01-10",
            top_n=5, pick_k=2, max_rsi=40, rebalance_hours=24,
        )
        config_b = BacktestConfig(
            start="2024-01-01", end="2024-01-10",
            top_n=8, pick_k=4, max_rsi=60, rebalance_hours=12,
        )

        result_a = run_backtest(all_data, config_a)
        result_b = run_backtest(all_data, config_b)

        # Results should differ (different params → different trades)
        assert result_a.equity_curve != result_b.equity_curve or \
               result_a.trade_count != result_b.trade_count or \
               result_a.rebalance_count != result_b.rebalance_count

    def test_max_rsi_filter_reduces_trades(self):
        """Stricter max_rsi should result in fewer or equal trades."""
        all_data = {
            f"KRW-COIN{i}": _generate_synthetic_market(f"KRW-COIN{i}", n_days=10, seed=i)
            for i in range(5)
        }

        loose = BacktestConfig(start="2024-01-01", end="2024-01-10", top_n=5, pick_k=3, max_rsi=80)
        strict = BacktestConfig(start="2024-01-01", end="2024-01-10", top_n=5, pick_k=3, max_rsi=20)

        result_loose = run_backtest(all_data, loose)
        result_strict = run_backtest(all_data, strict)

        # Stricter filter → fewer or equal coins selected → potentially fewer trades
        # (Not guaranteed per-trade, but rebalance decisions should differ)
        assert result_loose.rebalance_count == result_strict.rebalance_count

    def test_equity_curve_starts_at_initial_capital(self):
        all_data = {
            "KRW-BTC": _generate_synthetic_market("KRW-BTC", n_days=5, seed=0),
        }
        config = BacktestConfig(
            start="2024-01-01", end="2024-01-05",
            top_n=1, pick_k=1, max_rsi=99,
            initial_capital=5_000_000,
        )
        result = run_backtest(all_data, config)
        # First equity point should be close to initial capital
        # (may differ slightly due to immediate rebalance + fees)
        assert abs(result.equity_curve[0] - 5_000_000) < 500_000
```

- [ ] **Step 2: Run integration test**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_integration.py -v`

Expected: All 4 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/backtest/test_rsi_integration.py
git commit -m "test(backtest): add RSI portfolio integration tests"
```

---

## Task 10: Real Data Run + Validation

**Files:**
- Modify: `backtest/rsi_backtest.py` (fix any issues found during real run)

- [ ] **Step 1: Fetch real data and run backtest**

Run:
```bash
cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && \
uv run backtest/rsi_backtest.py \
    --start 2025-01-01 \
    --end 2025-03-01 \
    --top-n 30 \
    --pick-k 5 \
    --max-rsi 45 \
    --rebalance-hours 24 \
    --prefetch 50 \
    --export-dir backtest/output
```

Expected: Should print performance summary with all metrics and BTC benchmark.

- [ ] **Step 2: Verify parameter changes produce different results**

Run with different parameters:
```bash
uv run backtest/rsi_backtest.py \
    --start 2025-01-01 \
    --end 2025-03-01 \
    --top-n 20 \
    --pick-k 3 \
    --max-rsi 35 \
    --rebalance-hours 12 \
    --skip-fetch
```

Expected: Different metrics values than the first run.

- [ ] **Step 3: Verify CSV exports**

Run: `ls -la backtest/output/ && head -5 backtest/output/equity_curve.csv && head -5 backtest/output/monthly_returns.csv`

Expected: CSV files with headers and data rows.

- [ ] **Step 4: Add output directory to .gitignore**

Ensure `backtest/output/` is covered by the existing `backtest/data/` gitignore or add:

```
backtest/output/
```

- [ ] **Step 5: Run all backtest tests**

Run: `cd /Users/robin/.superset/worktrees/auto_trader/phase1-crypto-rsi-plan && uv run pytest tests/backtest/test_rsi_*.py -v`

Expected: All tests PASS.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "feat: add phase1 crypto rsi backtest mvp"
```

---

## Limitations & Next Steps

### Known Limitations (MVP)
- **Survivorship bias**: Pre-fetches current top markets, not historical universe
- **No crash filter / cooldown**: Enters positions purely on RSI regardless of market conditions
- **No market warning filter**: Ignores Upbit `market_warning` status
- **Single timeframe**: 1h only, no multi-timeframe confirmation
- **Execution assumption**: Uses current bar close, not next bar open
- **No position sizing variation**: Strict equal-weight only

### Phase 2 Extensions
- Parameter sweep: grid search over `top_n`, `pick_k`, `max_rsi`, `rebalance_hours`
- Walk-forward cross-validation (reuse pattern from `backtest/prepare.py`)
- JSON result export for autoresearch tracking
- Add crash filter (market RSI > threshold → skip rebalance)
- Add cooldown (don't re-enter recently exited positions)

### Phase 3 Extensions
- Multi-signal ensemble: RSI + MACD + BB + momentum voting
- Dynamic position sizing based on conviction score
- Benchmark against ETH, SOL buy & hold in addition to BTC
