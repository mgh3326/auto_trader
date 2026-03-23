# Multi-Signal Voting System → Live Integration

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port the backtest-validated multi-signal voting system (Sharpe 2.45, score 2.45 from PR #381) into the live MCP crypto screening and holdings paths. Currently live uses RSI-only; this adds MACD, Bollinger Bands, EMA crossover, Momentum, and Volume signals with vote-based buy/sell decisions.

**Architecture:** Add a shared `CryptoVotingSignals` evaluator that both `screen_stocks` and `get_holdings` can call. The evaluator takes OHLCV data and returns bull/bear vote counts plus individual signal flags. Screen results include vote counts for AI-assisted decision making. Holdings strategy signals upgrade from simple stop-loss/mean-reversion to the full voting system.

**Tech Stack:** Python 3.13, FastMCP tooling, pandas, numpy, pytest, Ruff.

**Reference:** `backtest/strategy.py` contains the validated parameters and logic.

---

## Validated Parameters (from backtest PR #381)

```python
# RSI
RSI_PERIOD_FAST = 7
RSI_PERIOD_SLOW = 14
RSI_OVERSOLD = 30
RSI_EXIT = 46

# Multi-Signal Voting
MIN_VOTES = 4          # Buy requires ≥4 of 6 bull signals
MIN_SELL_VOTES = 2     # Sell on ≥2 of 5 bear signals

# Indicators
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 15
BB_STD = 2.0
EMA_FAST = 8
EMA_SLOW = 24
MOMENTUM_PERIOD = 5
VOLUME_LOOKBACK = 20
VOLUME_THRESHOLD = 1.5

# Exit
STOP_LOSS_PCT = 0.04
COOLDOWN_DAYS = 12     # backtest uses 12, live uses 8 — keep live at 8 for now
```

## Bull Signals (6 total, need ≥4 to buy)
1. `dual_rsi_oversold` — RSI14 ≤ 30 AND RSI7 ≤ 30
2. `macd_histogram_positive` — MACD histogram > 0
3. `close_below_bb_lower` — price < lower Bollinger Band
4. `ema_fast_above_slow` — EMA8 > EMA24
5. `momentum_positive` — 5-day momentum > 0%
6. `volume_above_avg` — current volume > 1.5× 20-day average

## Bear Signals (5 total, need ≥2 to sell)
1. `macd_histogram_negative` — MACD histogram < 0
2. `close_above_bb_upper` — price > upper Bollinger Band
3. `ema_fast_below_slow` — EMA8 < EMA24
4. `momentum_negative` — 5-day momentum < 0%
5. `rsi_slow_high` — RSI14 > 46

---

### Task 1: Create shared voting signal evaluator module

**Files:**
- Create: `app/services/crypto_voting_signals.py`
- Test: `tests/test_crypto_voting_signals.py`

**Step 1: Write the failing tests**

Test the evaluator with known OHLCV data:

```python
import numpy as np
import pytest
from app.services.crypto_voting_signals import CryptoVotingSignals, VotingResult


@pytest.fixture
def evaluator():
    return CryptoVotingSignals()


def _make_ohlcv_df(closes: list[float], volumes: list[float] | None = None):
    """Create a minimal OHLCV DataFrame for testing."""
    import pandas as pd
    n = len(closes)
    if volumes is None:
        volumes = [1000.0] * n
    return pd.DataFrame({
        "open": closes,  # simplified
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": volumes,
    })


class TestCryptoVotingSignals:
    def test_insufficient_history_returns_none(self, evaluator):
        df = _make_ohlcv_df([100.0] * 10)  # too few bars
        result = evaluator.evaluate(df)
        assert result is None

    def test_oversold_with_volume_spike_gives_high_bull_votes(self, evaluator):
        # Create data that produces RSI < 30, volume spike, etc.
        # Descending prices = oversold RSI
        closes = list(np.linspace(200, 100, 50))
        volumes = [1000.0] * 30 + [5000.0] * 20  # volume spike at end
        df = _make_ohlcv_df(closes, volumes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert result.bull_votes >= 1  # at minimum dual_rsi should fire
        assert isinstance(result.bull_flags, dict)
        assert isinstance(result.bear_flags, dict)

    def test_result_has_all_fields(self, evaluator):
        closes = list(np.linspace(100, 200, 50)) + list(np.linspace(200, 150, 10))
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert hasattr(result, 'rsi_fast')
        assert hasattr(result, 'rsi_slow')
        assert hasattr(result, 'bull_votes')
        assert hasattr(result, 'bear_votes')
        assert hasattr(result, 'bull_flags')
        assert hasattr(result, 'bear_flags')
        assert hasattr(result, 'buy_signal')
        assert hasattr(result, 'sell_signal')
        assert len(result.bull_flags) == 6
        assert len(result.bear_flags) == 5

    def test_buy_signal_requires_min_votes(self, evaluator):
        # MIN_VOTES = 4, so < 4 bull votes = no buy
        closes = list(np.linspace(100, 200, 50))  # uptrend = not oversold
        df = _make_ohlcv_df(closes)
        result = evaluator.evaluate(df)
        assert result is not None
        assert result.buy_signal is False  # uptrend won't trigger enough bull signals
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_crypto_voting_signals.py -q`

**Step 3: Write the implementation**

Port the indicator calculations from `backtest/strategy.py` into a reusable service:

```python
"""Crypto multi-signal voting system for live trading decisions.

Ported from backtest/strategy.py (PR #381, Sharpe 2.45).
Evaluates 6 bull signals and 5 bear signals from OHLCV data.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Validated parameters from backtest PR #381
RSI_PERIOD_FAST = 7
RSI_PERIOD_SLOW = 14
RSI_OVERSOLD = 30
RSI_EXIT = 46

MIN_VOTES = 4
MIN_SELL_VOTES = 2

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 15
BB_STD = 2.0
EMA_FAST = 8
EMA_SLOW = 24
MOMENTUM_PERIOD = 5
VOLUME_LOOKBACK = 20
VOLUME_THRESHOLD = 1.5

# Minimum bars needed for all indicators
MIN_HISTORY_BARS = max(RSI_PERIOD_SLOW, BB_PERIOD, EMA_SLOW, MACD_SLOW + MACD_SIGNAL) + 1


@dataclasses.dataclass(frozen=True)
class VotingResult:
    rsi_fast: float | None
    rsi_slow: float | None
    bull_votes: int
    bear_votes: int
    bull_flags: dict[str, bool]
    bear_flags: dict[str, bool]
    buy_signal: bool  # bull_votes >= MIN_VOTES
    sell_signal: bool  # bear_votes >= MIN_SELL_VOTES

    def to_dict(self) -> dict[str, Any]:
        return {
            "rsi_fast": self.rsi_fast,
            "rsi_slow": self.rsi_slow,
            "bull_votes": self.bull_votes,
            "bear_votes": self.bear_votes,
            "bull_flags": self.bull_flags,
            "bear_flags": self.bear_flags,
            "buy_signal": self.buy_signal,
            "sell_signal": self.sell_signal,
        }


class CryptoVotingSignals:
    """Evaluate multi-signal voting for crypto positions/candidates."""

    def evaluate(self, df: pd.DataFrame) -> VotingResult | None:
        """Evaluate all signals from OHLCV DataFrame.

        Args:
            df: DataFrame with columns: open, high, low, close, volume.
                Must have at least MIN_HISTORY_BARS rows.

        Returns:
            VotingResult or None if insufficient data.
        """
        if df is None or len(df) < MIN_HISTORY_BARS:
            return None

        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float)
        current_close = closes[-1]
        current_volume = volumes[-1]

        # Calculate indicators
        rsi_fast = _calc_rsi(closes, RSI_PERIOD_FAST)
        rsi_slow = _calc_rsi(closes, RSI_PERIOD_SLOW)
        if rsi_slow is None:
            return None

        macd_result = _calc_macd(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        bb_result = _calc_bollinger(closes, BB_PERIOD, BB_STD)
        ema_fast = _calc_ema(closes, EMA_FAST)
        ema_slow = _calc_ema(closes, EMA_SLOW)
        momentum = _calc_momentum(closes, MOMENTUM_PERIOD)
        avg_volume = _calc_average_volume(volumes, VOLUME_LOOKBACK)

        # Bull signals (6)
        bull_flags = {
            "dual_rsi_oversold": (
                rsi_slow <= RSI_OVERSOLD
                and rsi_fast is not None
                and rsi_fast <= RSI_OVERSOLD
            ),
            "macd_histogram_positive": (
                macd_result is not None and macd_result[2] > 0
            ),
            "close_below_bb_lower": (
                bb_result is not None and current_close < bb_result[2]
            ),
            "ema_fast_above_slow": (
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] > ema_slow[-1]
            ),
            "momentum_positive": momentum is not None and momentum > 0,
            "volume_above_avg": (
                avg_volume is not None
                and current_volume > avg_volume * VOLUME_THRESHOLD
            ),
        }
        bull_votes = sum(1 for v in bull_flags.values() if v)

        # Bear signals (5)
        bear_flags = {
            "macd_histogram_negative": (
                macd_result is not None and macd_result[2] < 0
            ),
            "close_above_bb_upper": (
                bb_result is not None and current_close > bb_result[0]
            ),
            "ema_fast_below_slow": (
                ema_fast is not None
                and ema_slow is not None
                and ema_fast[-1] < ema_slow[-1]
            ),
            "momentum_negative": momentum is not None and momentum < 0,
            "rsi_slow_high": rsi_slow > RSI_EXIT,
        }
        bear_votes = sum(1 for v in bear_flags.values() if v)

        return VotingResult(
            rsi_fast=rsi_fast,
            rsi_slow=rsi_slow,
            bull_votes=bull_votes,
            bear_votes=bear_votes,
            bull_flags=bull_flags,
            bear_flags=bear_flags,
            buy_signal=bull_votes >= MIN_VOTES,
            sell_signal=bear_votes >= MIN_SELL_VOTES,
        )


# --- Indicator functions (ported from backtest/strategy.py) ---


def _calc_rsi(closes: np.ndarray, period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    return float(100 - (100 / (1 + rs)))


def _calc_ema(closes: np.ndarray, span: int) -> np.ndarray | None:
    if len(closes) < span:
        return None
    return pd.Series(closes).ewm(span=span, adjust=False).mean().values


def _calc_macd(
    closes: np.ndarray, fast: int, slow: int, signal: int
) -> tuple[float, float, float] | None:
    if len(closes) < slow + signal:
        return None
    ema_fast = pd.Series(closes).ewm(span=fast, adjust=False).mean()
    ema_slow = pd.Series(closes).ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def _calc_bollinger(
    closes: np.ndarray, period: int, std_mult: float
) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    middle = np.mean(closes[-period:])
    std = np.std(closes[-period:])
    return float(middle + std_mult * std), float(middle), float(middle - std_mult * std)


def _calc_momentum(closes: np.ndarray, period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    return float((closes[-1] - closes[-(period + 1)]) / closes[-(period + 1)] * 100)


def _calc_average_volume(volumes: np.ndarray, lookback: int) -> float | None:
    if len(volumes) < lookback:
        return None
    return float(np.mean(volumes[-lookback:]))
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_crypto_voting_signals.py -q`

---

### Task 2: Integrate voting signals into crypto screening enrichment

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py`
- Modify: `app/mcp_server/tooling/screening/enrichment.py` (if needed)
- Test: `tests/test_mcp_screen_stocks_crypto.py`

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_screen_crypto_includes_voting_signals(mock_upbit_tickers, ...):
    """Screen results should include bull_votes, bear_votes, buy_signal."""
    result = await _screen_crypto(market="crypto", ...)
    items = result.get("results", [])
    assert len(items) > 0
    for item in items:
        # Voting fields should be present (may be None if enrichment failed)
        assert "bull_votes" in item
        assert "bear_votes" in item
        assert "buy_signal" in item
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_crypto.py -k voting -q`

**Step 3: Modify enrichment to add voting signals**

In `_run_crypto_indicator_enrichment` or `_enrich_single_item`, after fetching OHLCV:

```python
from app.services.crypto_voting_signals import CryptoVotingSignals

_voting_evaluator = CryptoVotingSignals()


async def _enrich_single_item(item: dict[str, Any]) -> None:
    # ... existing OHLCV fetch ...
    df = await asyncio.wait_for(
        _fetch_ohlcv_for_indicators(symbol, "crypto", count=50),
        timeout=_timeout_seconds("crypto_enrichment"),
    )

    # Existing enrichment
    metrics = calculate_crypto_metrics_from_ohlcv(df)
    item["volume_ratio"] = metrics.get("volume_ratio")
    # ... etc ...

    # NEW: Voting signal enrichment
    voting_result = _voting_evaluator.evaluate(df)
    if voting_result is not None:
        item["bull_votes"] = voting_result.bull_votes
        item["bear_votes"] = voting_result.bear_votes
        item["buy_signal"] = voting_result.buy_signal
        item["sell_signal"] = voting_result.sell_signal
        item["bull_flags"] = voting_result.bull_flags
    else:
        item["bull_votes"] = None
        item["bear_votes"] = None
        item["buy_signal"] = None
        item["sell_signal"] = None
        item["bull_flags"] = None
```

**Important:** The OHLCV count must be at least `MIN_HISTORY_BARS` (36 bars). Current `count=50` is sufficient.

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_crypto.py -k voting -q`

---

### Task 3: Upgrade holdings strategy signals with voting system

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Test: `tests/test_mcp_portfolio_tools.py`

**Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_holdings_strategy_signal_includes_voting(monkeypatch, ...):
    """Holdings with strategy_signals=True should include voting data."""
    # Mock a crypto position with OHLCV available
    result = await get_holdings(account="upbit", strategy_signals=True)
    positions = result.get("positions", [])
    crypto_pos = next((p for p in positions if "KRW-" in str(p.get("symbol", ""))), None)
    if crypto_pos and crypto_pos.get("strategy_signal"):
        signal = crypto_pos["strategy_signal"]
        assert "bull_votes" in signal or signal.get("reason") in ("stop_loss", "mean_reversion_exit")
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_portfolio_tools.py -k voting -q`

**Step 3: Enhance `_build_crypto_strategy_signal`**

Upgrade the existing function to include voting data alongside the existing stop-loss/mean-reversion logic:

```python
from app.services.crypto_voting_signals import CryptoVotingSignals, VotingResult

_voting_evaluator = CryptoVotingSignals()


def _build_crypto_strategy_signal(
    position: dict[str, Any],
    *,
    rsi_14: float | None,
    voting_result: VotingResult | None = None,
) -> dict[str, Any] | None:
    profit_rate = _to_optional_float(position.get("profit_rate"))
    if profit_rate is None:
        return None

    # Stop-loss takes priority (unchanged)
    if profit_rate <= CRYPTO_STOP_LOSS_PCT:
        signal = {
            "action": "sell",
            "reason": "stop_loss",
            "threshold_pct": CRYPTO_STOP_LOSS_PCT,
        }
        if voting_result:
            signal["bear_votes"] = voting_result.bear_votes
        return signal

    # Mean-reversion exit when profitable and RSI > 46 (unchanged)
    if profit_rate > 0 and rsi_14 is not None and rsi_14 > CRYPTO_MEAN_REVERSION_RSI_EXIT:
        signal = {
            "action": "sell",
            "reason": "mean_reversion_exit",
            "rsi_14": rsi_14,
        }
        if voting_result:
            signal["bear_votes"] = voting_result.bear_votes
        return signal

    # NEW: Bear vote exit (when ≥2 bear signals and in loss)
    if voting_result and voting_result.sell_signal and profit_rate < 0:
        return {
            "action": "sell",
            "reason": "bear_vote_exit",
            "bear_votes": voting_result.bear_votes,
            "bear_flags": voting_result.bear_flags,
        }

    # No sell signal — return voting context for informational purposes
    if voting_result:
        return {
            "action": "hold",
            "reason": "voting_status",
            "bull_votes": voting_result.bull_votes,
            "bear_votes": voting_result.bear_votes,
            "buy_signal": voting_result.buy_signal,
            "sell_signal": voting_result.sell_signal,
        }

    return None
```

Also update `_compute_crypto_rsi_for_position` to return voting result alongside RSI:

```python
async def _compute_crypto_signals_for_position(
    position: dict[str, Any],
) -> tuple[float | None, VotingResult | None]:
    """Compute RSI and voting signals for a crypto position."""
    symbol = str(position.get("symbol") or "").strip()
    if not symbol:
        return None, None
    try:
        df = await _fetch_ohlcv_for_indicators(symbol, "crypto", count=50)
    except Exception:
        return None, None
    if df.empty:
        return None, None

    # RSI from the existing method
    rsi = _calc_rsi_from_df(df)  # extract existing RSI calc

    # Voting signals
    voting = _voting_evaluator.evaluate(df)
    return rsi, voting
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_portfolio_tools.py -k "voting or strategy" -q`

---

### Task 4: Add voting fields to finalize_crypto_screen output

**Files:**
- Modify: `app/mcp_server/tooling/analysis_screen_crypto.py`

**Step 1: Ensure voting fields survive finalization**

The `finalize_crypto_screen` function formats and filters the final output. Make sure `bull_votes`, `bear_votes`, `buy_signal`, `sell_signal`, `bull_flags` are included in the output schema and not dropped during field selection.

Check the output field whitelist and add voting fields:

```python
# In the output formatting section
CRYPTO_OUTPUT_FIELDS = [
    "symbol", "name", "close", "change_rate", "trade_amount_24h",
    "volume_24h", "market_cap", "market_cap_rank",
    "rsi", "rsi_bucket", "adx", "volume_ratio", "candle_type",
    # NEW: Voting signals
    "bull_votes", "bear_votes", "buy_signal", "sell_signal", "bull_flags",
]
```

**Step 2: Run full test suite**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_portfolio_tools.py -q`

---

### Task 5: Integration tests and validation

**Files:**
- Test: `tests/test_crypto_voting_integration.py` (new)

**Step 1: Write integration tests**

```python
"""Integration tests: voting signals consistency between backtest and live."""

import numpy as np
import pandas as pd
import pytest

from app.services.crypto_voting_signals import CryptoVotingSignals


class TestVotingBacktestConsistency:
    """Verify live voting evaluator matches backtest behavior."""

    def test_same_parameters_as_backtest(self):
        from app.services.crypto_voting_signals import (
            RSI_PERIOD_FAST, RSI_PERIOD_SLOW, RSI_OVERSOLD, RSI_EXIT,
            MIN_VOTES, MIN_SELL_VOTES,
            MACD_FAST, MACD_SLOW, MACD_SIGNAL,
            BB_PERIOD, BB_STD, EMA_FAST, EMA_SLOW,
            MOMENTUM_PERIOD, VOLUME_LOOKBACK, VOLUME_THRESHOLD,
        )
        # These must match backtest/strategy.py
        assert RSI_PERIOD_FAST == 7
        assert RSI_PERIOD_SLOW == 14
        assert RSI_OVERSOLD == 30
        assert RSI_EXIT == 46
        assert MIN_VOTES == 4
        assert MIN_SELL_VOTES == 2
        assert MACD_FAST == 12
        assert MACD_SLOW == 26
        assert MACD_SIGNAL == 9
        assert BB_PERIOD == 15
        assert BB_STD == 2.0
        assert EMA_FAST == 8
        assert EMA_SLOW == 24
        assert MOMENTUM_PERIOD == 5
        assert VOLUME_LOOKBACK == 20
        assert VOLUME_THRESHOLD == 1.5

    def test_bull_signal_count_is_six(self):
        evaluator = CryptoVotingSignals()
        # Create minimal valid data
        closes = list(np.linspace(200, 100, 50))
        df = pd.DataFrame({
            "open": closes, "high": [c*1.01 for c in closes],
            "low": [c*0.99 for c in closes], "close": closes,
            "volume": [1000.0] * 50,
        })
        result = evaluator.evaluate(df)
        assert result is not None
        assert len(result.bull_flags) == 6

    def test_bear_signal_count_is_five(self):
        evaluator = CryptoVotingSignals()
        closes = list(np.linspace(100, 200, 50))
        df = pd.DataFrame({
            "open": closes, "high": [c*1.01 for c in closes],
            "low": [c*0.99 for c in closes], "close": closes,
            "volume": [1000.0] * 50,
        })
        result = evaluator.evaluate(df)
        assert result is not None
        assert len(result.bear_flags) == 5
```

**Step 2: Run all tests**

Run: `uv run pytest tests/ -q -k "voting or crypto_voting or strategy_signal"`

**Step 3: Run ruff**

Run: `uv run ruff check app/services/crypto_voting_signals.py app/mcp_server/tooling/screening/crypto.py app/mcp_server/tooling/portfolio_holdings.py`

---

## Summary of Changes

| File | Change |
|------|--------|
| `app/services/crypto_voting_signals.py` | **NEW** — Shared voting evaluator (ported from backtest) |
| `app/mcp_server/tooling/screening/crypto.py` | Add voting fields during enrichment |
| `app/mcp_server/tooling/portfolio_holdings.py` | Upgrade strategy signals with voting |
| `app/mcp_server/tooling/analysis_screen_crypto.py` | Include voting fields in output |
| `tests/test_crypto_voting_signals.py` | **NEW** — Unit tests for evaluator |
| `tests/test_crypto_voting_integration.py` | **NEW** — Parameter consistency tests |
| `tests/test_mcp_screen_stocks_crypto.py` | Add voting field assertions |
| `tests/test_mcp_portfolio_tools.py` | Add voting strategy signal tests |

## ⚠️ Important Notes

1. **OHLCV count**: Enrichment currently fetches `count=50` bars. MIN_HISTORY_BARS is 36. 50 is sufficient, no change needed.
2. **Cooldown days**: Backtest uses 12, live uses 8. Keep live at 8 — the discrepancy is acceptable (conservative in live).
3. **Performance**: Voting evaluation adds negligible CPU overhead since OHLCV is already fetched for RSI enrichment.
4. **Backward compatibility**: All new fields are additive. Existing API consumers see new fields but nothing breaks.
5. **buy_signal in screen is informational**: The actual buy decision still goes through `place_order` which has its own validation. The `buy_signal` flag helps the AI caller make better recommendations.
