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

The backtest uses fixed date splits (based on available data):

- **Train**: 2025-03-22 to 2025-06-30
- **Validation**: 2025-07-01 to 2025-09-30 (default for `backtest.py`)
- **Test**: 2025-10-01 to 2026-03-22

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

*Scores recorded on 2026-03-22 using BTC, ETH, SOL data*

| Strategy | Score | Return % | Sharpe | Max DD % | Trades |
|----------|-------|----------|--------|----------|--------|
| RSI (val split) | TBD | TBD | TBD | TBD | TBD |
| Buy & Hold | TBD | TBD | TBD | TBD | TBD |
| Random | TBD | TBD | TBD | TBD | TBD |

Run `uv run backtest/backtest.py` with each strategy to populate baseline scores.

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
