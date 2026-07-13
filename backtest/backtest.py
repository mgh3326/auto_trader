"""Backtest runner."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

# Add backtest directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

import prepare
import report

from research_contracts.canonical_hash import canonical_sha256

STRATEGY_PATH = Path(__file__).resolve().parent / "strategy.py"


class StrategySourceMismatch(RuntimeError):
    """The child process observed strategy bytes unlike the registered identity."""


def load_verified_strategy_class(
    expected_strategy_sha256: str | None,
    expected_params_sha256: str | None,
    *,
    strategy_path: Path = STRATEGY_PATH,
) -> type:
    """Hash candidate bytes before executing those exact bytes in this child."""
    source = strategy_path.read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    if expected_strategy_sha256 is not None and source_hash != expected_strategy_sha256:
        raise StrategySourceMismatch(
            "strategy_source_hash_mismatch: "
            f"expected {expected_strategy_sha256}, observed {source_hash}"
        )

    module_name = "_verified_backtest_strategy"
    module = ModuleType(module_name)
    module.__file__ = str(strategy_path)
    module.__package__ = None
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(strategy_path), "exec"), module.__dict__)
    finally:
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:  # pragma: no cover - defensive restoration for embedding callers
            sys.modules[module_name] = previous_module
    params = getattr(module, "PARAMS", None)
    if (
        expected_params_sha256 is not None
        and canonical_sha256(params) != expected_params_sha256
    ):
        raise StrategySourceMismatch(
            "strategy_params_hash_mismatch: executable PARAMS differ from registration"
        )
    strategy_class = getattr(module, "Strategy", None)
    if not isinstance(strategy_class, type):
        raise RuntimeError("verified strategy.py does not define class Strategy")
    return strategy_class


def main() -> None:
    """Run backtest with configurable mode."""
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument(
        "--mode",
        choices=["single", "cv", "report"],
        default="single",
        help=(
            "single: run on one split (default). "
            "cv: walk-forward cross-validation. "
            "report: detailed split + CV report."
        ),
    )
    parser.add_argument(
        "--split",
        choices=["train", "val", "test"],
        default="val",
        help="Split to use in single mode (default: val)",
    )
    parser.add_argument(
        "--interval",
        default="1d",
        help="Bar interval to load (default: 1d)",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Report output format (default: text). Used with --mode report.",
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=prepare.TRADING_FEE * 10_000,
        help="Taker fee charged at each fill, in basis points.",
    )
    parser.add_argument(
        "--half-spread-bps",
        type=float,
        default=prepare.HALF_SPREAD_BPS,
        help="Half-spread charged at each fill, in basis points.",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=prepare.SLIPPAGE_BPS,
        help="Slippage charged at each fill, in basis points.",
    )
    parser.add_argument(
        "--expected-strategy-sha256",
        help="Registered strategy.py SHA-256; verified before candidate import",
    )
    parser.add_argument(
        "--expected-params-sha256",
        help="Registered PARAMS canonical SHA-256; verified after child import",
    )
    args = parser.parse_args()
    strategy_class = load_verified_strategy_class(
        args.expected_strategy_sha256,
        args.expected_params_sha256,
    )
    execution_cost = prepare.ExecutionCost(
        fee_rate=args.fee_bps / 10_000,
        half_spread_bps=args.half_spread_bps,
        slippage_bps=args.slippage_bps,
    )

    if args.mode == "cv":
        _run_cv(args.interval, execution_cost, strategy_class)
    elif args.mode == "report":
        _run_report(
            args.split,
            args.interval,
            args.output,
            execution_cost,
            strategy_class,
        )
    else:
        _run_single(args.split, args.interval, execution_cost, strategy_class)


def _run_single(
    split: str,
    bar_interval: str,
    execution_cost: prepare.ExecutionCost,
    strategy_class: type,
) -> None:
    """Run backtest on a single split."""
    print(f"Loading {split} data...")
    data = prepare.load_data(split, bar_interval=bar_interval)

    if not data:
        print("No data available. Run fetch_data.py first.")
        sys.exit(1)

    print(f"Loaded {len(data)} symbols: {', '.join(data.keys())}")

    strat = strategy_class()

    print("Running backtest...")
    result = prepare.run_backtest(
        data,
        strat,
        bar_interval=bar_interval,
        execution_cost=execution_cost,
    )

    total_bars = sum(len(df) for df in data.values())
    print(f"\nLoaded {total_bars} bars across {len(data)} symbols")
    _print_result(result)


def _run_cv(
    bar_interval: str,
    execution_cost: prepare.ExecutionCost,
    strategy_class: type,
) -> None:
    """Run walk-forward cross-validation."""
    print("Running walk-forward cross-validation...")
    print(f"Folds: {len(prepare.CV_FOLDS)}")

    cv_result = prepare.cross_validate(
        strategy_class,
        bar_interval=bar_interval,
        execution_cost=execution_cost,
    )

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
    try:
        trial = prepare.summarize_trial_statistics(cv_result.fold_results)
    except prepare.TrialStatisticsError:
        print("trial_sharpe:       nan")
        print("trial_p_value:      nan")
        print(f"trial_sample_size:  {len(cv_result.fold_results)}")
    else:
        print(f"trial_sharpe:       {trial.sharpe:.6f}")
        print(f"trial_p_value:      {trial.p_value:.12f}")
        print(f"trial_sample_size:  {trial.sample_size}")
    print("=" * 50)


def _run_report(
    split: str,
    bar_interval: str,
    output: str,
    execution_cost: prepare.ExecutionCost,
    strategy_class: type,
) -> None:
    """Run detailed report for a single split plus cross-validation."""
    data = prepare.load_data(split, bar_interval=bar_interval)

    if not data:
        print("No data available. Run fetch_data.py first.")
        sys.exit(1)

    single_result = prepare.run_backtest(
        data,
        strategy_class(),
        bar_interval=bar_interval,
        execution_cost=execution_cost,
    )
    cv_result = prepare.cross_validate(
        strategy_class,
        bar_interval=bar_interval,
        execution_cost=execution_cost,
    )
    split_dates = prepare.SPLITS[split]
    rendered = report.generate_report(
        single_result,
        data=data,
        split_info={
            "name": split,
            "start": split_dates["start"],
            "end": split_dates["end"],
        },
        cv_result=cv_result,
        output=output,
    )

    if output == "json":
        print(json.dumps(rendered, ensure_ascii=False, indent=2))
        return

    print(rendered)


def _print_result(result: Any) -> None:
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
    print(f"time_in_market_pct: {result.time_in_market_pct:.1f}%")
    print(f"backtest_seconds:   {result.backtest_seconds:.3f}")
    print("=" * 40)


if __name__ == "__main__":
    main()
