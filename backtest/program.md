# Backtest Module Program Guide

## Overview

This backtest module provides a fixed-file backtesting framework for Upbit spot crypto daily bars. It supports deterministic backtesting with a pluggable strategy interface.

## Architecture

### Fixed Files (Do Not Modify)

- **`prepare.py`** - Backtest engine with deterministic execution, metrics calculation, and data loading
- **`backtest.py`** - Fixed entry point runner that loads data, runs strategy, and prints results
- **`fetch_data.py`** - Upbit daily candle backfill script with incremental updates

### Mutable File (Modify This)

- **`strategy.py`** - Your trading strategy implementation. Edit this file to experiment with different approaches.

### Benchmarks (Reference Only)

- **`benchmarks/buy_and_hold.py`** - Buy-and-hold baseline
- **`benchmarks/random_baseline.py`** - Random action baseline for comparison

## Quick Start

### 1. Backfill Data

Fetch historical data for the default symbols:

```bash
uv run backtest/fetch_data.py --symbols BTC ETH SOL --days 365
```

Or fetch top 100 markets by 24h traded value:

```bash
uv run backtest/fetch_data.py --top-n 100 --days 730
```

### 2. Run Backtest

```bash
uv run backtest/backtest.py
```

This will:
- Load the validation split data
- Run your strategy from `strategy.py`
- Print performance metrics and score

Warmup note:
- Trades are evaluated only on the selected split dates.
- `BarData.history` may include pre-split rows so indicators can warm up before the first validation/test bar.

### 3. Iterate on Strategy

Edit `backtest/strategy.py` to modify the trading logic. The fixed runner ensures fair comparison between iterations.

## Strategy Interface

Your strategy class must implement:

```python
def on_bar(
    self,
    bar_data: dict[str, BarData],        # Symbol -> BarData with history
    portfolio: PortfolioState,            # Current portfolio state
) -> list[Signal]:                       # List of signals to execute
```

### BarData Fields

- `symbol` - Symbol name
- `date` - Current date (YYYY-MM-DD)
- `open`, `high`, `low`, `close` - OHLC prices
- `volume` - Trading volume
- `value` - Trading value (volume * price)
- `history` - DataFrame with LOOKBACK_BARS of history including current bar

### Signal Format

```python
Signal(
    symbol="BTC",        # Symbol to trade
    action="buy",        # "buy" or "sell"
    weight=0.15,         # Target weight for buy, fraction to sell for sell
    reason="RSI < 30",   # Reason for the signal (optional)
)
```

**Buy signals:** `weight` is the target portfolio weight (0-1).
**Sell signals:** `weight` is the fraction of current position to sell (1.0 = full liquidation).

### PortfolioState Fields

- `cash` - Available cash
- `positions` - Dict of symbol -> quantity held
- `avg_prices` - Dict of symbol -> average entry price
- `position_dates` - Dict of symbol -> entry date (use for holding period)
- `equity` - Current portfolio equity (cash + position values)
- `date` - Current date
- `trade_log` - List of executed trades

## Data Splits

The backtest uses fixed date splits (revised 2026-03-22 for balanced RSI signal coverage):

- **Train**: 2024-04-01 to 2025-06-30 (451 days, RSI<30=12, bull+bear mix)
- **Validation**: 2025-07-01 to 2026-01-31 (214 days, RSI<30=16, default for `backtest.py`)
- **Test**: 2026-02-01 to 2026-03-22 (50 days, RSI<30=7, recent holdout)

## Metrics

The backtest computes:

- **Score** - Composite metric based on Sharpe with penalties
- **Total Return** - Percentage return over period
- **Sharpe Ratio** - Risk-adjusted return (annualized)
- **Max Drawdown** - Peak-to-trough decline percentage
- **Win Rate** - Percentage of profitable trades
- **Profit Factor** - Gross profit / gross loss
- **Avg Holding Days** - Mean position holding period
- **Backtest Seconds** - Runtime measurement

## Score Formula

```python
score = result.sharpe
if result.max_drawdown_pct > 20:
    score -= (result.max_drawdown_pct - 20) * 0.1
if result.num_trades < 10:
    score -= 1.0
```

## Default Universe

The fixed universe includes: BTC, ETH, SOL, XRP, LINK, ADA, DOT, AVAX

## Constants

Default values in `prepare.py`:

- `INITIAL_CAPITAL = 10_000_000` (10 million KRW)
- `TRADING_FEE = 0.0005` (0.05%)
- `SLIPPAGE_BPS = 2.0` (0.02%)
- `LOOKBACK_BARS = 200`

Default values in `strategy.py`:

- `RSI_PERIOD = 14`
- `RSI_OVERSOLD = 30`
- `RSI_OVERBOUGHT = 70`
- `MAX_POSITIONS = 5`
- `POSITION_SIZE = 0.15`
- `HOLDING_DAYS = 7`

## Allowed Libraries

You may use:
- pandas, numpy (data manipulation)
- Standard library modules

Do not add external dependencies without updating pyproject.toml.

## Development Workflow

1. Modify `strategy.py` with your approach
2. Run `uv run backtest/backtest.py` to see results
3. Compare score against previous iterations
4. Iterate until satisfied with performance

## Baseline Scores

*Recorded 2026-03-22, val split, BTC/ETH/SOL/XRP*

| Strategy | Score | Return % | Sharpe | Max DD % | Trades |
|----------|-------|----------|--------|----------|--------|
| RSI | -0.66 | -4.0% | -0.66 | 18.4% | 18 |
| Buy & Hold | ~-5.73 | -36.3% | -2.35 | 43.7% | 3 |
| Random | ~-1.77 | -13.7% | -1.77 | 19.6% | 19 |

**Your goal: beat RSI score of -0.66.**

## Autoresearch Loop (Phase 2)

### Setup
```bash
git checkout -b autotrader/<tag> main
echo -e "commit\tscore\tsharpe\tmax_dd\tstatus\tdescription" > results.tsv
```

### The Loop
```
LOOP FOREVER:
1. Read current strategy.py and previous scores
2. Propose a modification to strategy.py
3. git commit -m "exp<N>: description"
4. uv run backtest/backtest.py > run.log 2>&1
5. grep "^score:" run.log
6. If score IMPROVED (higher than best): keep
7. If score equal or worse: git reset --hard HEAD~1
8. Record in results.tsv
```

### Rules
- **Only edit strategy.py** — prepare.py, backtest.py, fetch_data.py are fixed
- **No new dependencies** — numpy, pandas, and stdlib only
- **Time budget** — 30 seconds per backtest max (RPi5 safe)

### Research Directions (Tier 1 — most likely to improve)
- RSI threshold tuning (30→35? 25?)
- Holding period optimization (7→14 days?)
- Position sizing (15%→20%? dynamic based on RSI depth?)
- Multi-timeframe RSI (7-day + 14-day agreement)
- Stop-loss addition (e.g., -10% from entry → forced sell)

### Research Directions (Tier 2 — medium risk)
- MACD crossover as confirmation signal
- Bollinger Band squeeze detection
- EMA trend filter (only buy when price > EMA50)
- Volume spike confirmation
- Correlation-aware position limits

### Research Directions (Tier 3 — exploratory)
- Multi-signal voting (MIN_VOTES like nunchi)
- Dynamic position sizing based on volatility
- Mean reversion with z-score
- Regime detection (trending vs ranging)

## Testing

Run the test suite:

```bash
uv run pytest tests/backtest -v
```

## Notes

- Data is stored in `backtest/data/` as Parquet files
- Results and logs are gitignored
- Keep the universe fixed for fair comparison
- The validation split is used for development; test split for final evaluation
- Universe selection uses 24h accumulated trade price (acc_trade_price_24h) for ranking
- Incremental refresh re-downloads only the stale gap plus an overlap window, then merges and deduplicates by `date`
