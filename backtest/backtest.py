"""Backtest runner."""

import argparse
import sys
from pathlib import Path

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare
import strategy


def main() -> None:
    """Run backtest with configurable mode."""
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument(
        "--mode",
        choices=["single", "cv"],
        default="single",
        help="single: run on one split (default). cv: walk-forward cross-validation.",
    )
    parser.add_argument(
        "--split",
        choices=["train", "val", "test"],
        default="val",
        help="Split to use in single mode (default: val)",
    )
    args = parser.parse_args()

    if args.mode == "cv":
        _run_cv()
    else:
        _run_single(args.split)


def _run_single(split: str) -> None:
    """Run backtest on a single split."""
    print(f"Loading {split} data...")
    data = prepare.load_data(split)

    if not data:
        print("No data available. Run fetch_data.py first.")
        sys.exit(1)

    print(f"Loaded {len(data)} symbols: {', '.join(data.keys())}")

    strat = strategy.Strategy()

    print("Running backtest...")
    result = prepare.run_backtest(data, strat)

    total_bars = sum(len(df) for df in data.values())
    print(f"\nLoaded {total_bars} bars across {len(data)} symbols")
    _print_result(result)


def _run_cv() -> None:
    """Run walk-forward cross-validation."""
    print("Running walk-forward cross-validation...")
    print(f"Folds: {len(prepare.CV_FOLDS)}")

    cv_result = prepare.cross_validate(strategy.Strategy)

    print("\n" + "=" * 50)
    print("CROSS-VALIDATION RESULTS")
    print("=" * 50)

    for j, (score, fold_result) in enumerate(
        zip(cv_result.fold_scores, cv_result.fold_results, strict=True)
    ):
        # Use fold_indices to get the correct fold metadata
        fold_idx = cv_result.fold_indices[j]
        fold = prepare.CV_FOLDS[fold_idx]
        print(f"\nFold {fold_idx + 1} [{fold['val_start']} ~ {fold['val_end']}]")
        print(f"  score:      {score:.4f}")
        print(f"  sharpe:     {fold_result.sharpe:.2f}")
        print(f"  return:     {fold_result.total_return_pct:.2f}%")
        print(f"  max_dd:     {fold_result.max_drawdown_pct:.2f}%")
        print(f"  trades:     {fold_result.num_trades}")

    print("\n" + "-" * 50)
    print(f"cv_score:           {cv_result.cv_score:.6f}")
    print(f"mean_score:         {cv_result.mean_score:.6f}")
    print(f"std_score:          {cv_result.std_score:.6f}")
    print(f"min_fold_score:     {cv_result.min_score:.6f}")
    print("=" * 50)


def _print_result(result: prepare.BacktestResult) -> None:
    """Print single backtest result."""
    score = prepare.compute_score(result)
    print("\n" + "=" * 40)
    print("BACKTEST RESULTS")
    print("=" * 40)
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
