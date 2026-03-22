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
    print("\n" + "=" * 40)
    print("BACKTEST RESULTS")
    print("=" * 40)
    print(f"Score:               {prepare.compute_score(result):.2f}")
    print(f"Total Return:        {result.total_return_pct:.2f}%")
    print(f"Sharpe Ratio:        {result.sharpe:.2f}")
    print(f"Max Drawdown:        {result.max_drawdown_pct:.2f}%")
    print(f"Number of Trades:    {result.num_trades}")
    print(f"Win Rate:            {result.win_rate * 100:.1f}%")
    print(f"Profit Factor:       {result.profit_factor:.2f}")
    print(f"Avg Holding Days:    {result.avg_holding_days:.1f}")
    print("=" * 40)


if __name__ == "__main__":
    main()
