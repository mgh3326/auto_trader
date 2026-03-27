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
