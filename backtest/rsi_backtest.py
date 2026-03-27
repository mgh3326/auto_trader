"""CLI entrypoint for crypto RSI portfolio backtest.

Usage:
    uv run backtest/rsi_backtest.py --start 2024-01-01 --end 2026-03-01
    uv run backtest/rsi_backtest.py --start 2024-06-01 --end 2025-06-01 --top-n 20 --pick-k 3 --max-rsi 35
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

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
