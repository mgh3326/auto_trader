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

Or fetch top 100 markets by volume:

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

### 3. Iterate on Strategy

Edit `backtest/strategy.py` to modify the trading logic. The fixed runner ensures fair comparison between iterations.

## Strategy Interface

Your strategy class must implement:

```python
def on_bar(
    self,
    date: str,                           # Current date (YYYY-MM-DD)
    bar_data: dict[str, BarData],        # Symbol -> OHLCV for current date
    portfolio: PortfolioState,            # Current portfolio state
    bar_index: int,                       # Index of current bar (0-based)
) -> list[Signal]:                       # List of signals to execute
```

### Signal Format

```python
Signal(
    symbol="BTC",        # Symbol to trade
    action="buy",        # "buy" or "sell"
    target_weight=0.15,  # Target portfolio weight (0-1)
)
```

### PortfolioState Fields

- `cash` - Available cash
- `positions` - Dict of symbol -> quantity held
- `avg_prices` - Dict of symbol -> average entry price
- `position_dates` - Dict of symbol -> entry date (use for holding period)
- `trade_log` - List of executed trades

## Data Splits

The backtest uses fixed date splits:

- **Train**: 2023-01-01 to 2024-06-30
- **Validation**: 2024-07-01 to 2024-12-31 (default for `backtest.py`)
- **Test**: 2025-01-01 to 2025-12-31

## Metrics

The backtest computes:

- **Score** - Composite metric with penalty for low trade count
- **Total Return** - Percentage return over period
- **Sharpe Ratio** - Risk-adjusted return (annualized)
- **Max Drawdown** - Peak-to-trough decline
- **Win Rate** - Percentage of profitable trades
- **Profit Factor** - Gross profit / gross loss
- **Avg Holding Days** - Mean position holding period

## Score Formula

```
score = (total_return + sharpe * 10 - max_drawdown * 0.5)
if num_trades < 10:
    score *= (num_trades / 10)  # Penalty for insufficient trades
```

## Default Universe

The fixed universe includes: BTC, ETH, SOL, XRP, DOGE

## Constants

Default values in `prepare.py`:

- `INITIAL_CAPITAL = 100_000.0`
- `TRADING_FEE = 0.0005` (0.05%)
- `SLIPPAGE_BPS = 10` (0.1%)

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

*Initial scores recorded on 2026-03-22 using BTC, ETH, SOL data from 2025-03-22 to 2026-03-22*

| Strategy | Score | Return % | Sharpe | Max DD % | Trades |
|----------|-------|----------|--------|----------|--------|
| RSI (initial) | -1.08 | 0.92% | 0.17 | 7.51% | 16 |
| Buy & Hold | TBD | TBD | TBD | TBD | TBD |
| Random | TBD | TBD | TBD | TBD | TBD |

**Strategy Summary:** RSI-based mean reversion with 14-period RSI, buying when RSI <= 30,
selling when RSI >= 70 or holding period >= 7 days with profit. Max 5 positions at 15% each.

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
