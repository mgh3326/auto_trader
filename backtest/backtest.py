"""Backtest runner."""

import sys
from pathlib import Path

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare
import strategy


def main() -> None:
    """Run backtest with fixed configuration."""
    # Load data
    print("Loading data...")
    data = prepare.load_data("val")

    if not data:
        print("No data available. Run fetch_data.py first.")
        sys.exit(1)

    print(f"Loaded {len(data)} symbols: {', '.join(data.keys())}")

    # Initialize strategy
    strat = strategy.Strategy()

    # Run backtest
    print("Running backtest...")
    result = prepare.run_backtest(data, strat)

    # Print metrics
    total_bars = sum(len(df) for df in data.values())
    print(f"\nLoaded {total_bars} bars across {len(data)} symbols")
    print("\n" + "=" * 40)
    print("BACKTEST RESULTS")
    print("=" * 40)
    score = prepare.compute_score(result)
    print(f"score:              {score:.6f}")
    print(f"total_return_pct:   {result.total_return_pct:.2f}%")
    print(f"sharpe:             {result.sharpe:.2f}")
    print(f"max_drawdown_pct:   {result.max_drawdown_pct:.2f}%")
    print(f"num_trades:         {result.num_trades}")
    print(f"win_rate_pct:       {result.win_rate_pct * 100:.1f}%")
    print(f"profit_factor:      {result.profit_factor:.2f}")
    print(f"avg_holding_days:   {result.avg_holding_days:.1f}")
    print(f"backtest_seconds:   {result.backtest_seconds:.3f}")
    print("=" * 40)


if __name__ == "__main__":
    main()
