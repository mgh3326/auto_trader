"""Performance metrics for backtest results."""

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from .simulator import BacktestResult


@dataclass
class Metrics:
    """Calculated performance metrics."""

    cumulative_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    trade_count: int
    turnover: float
    benchmark_return: float | None  # BTC buy & hold


def compute_metrics(
    result: BacktestResult,
    btc_data: pd.DataFrame | None = None,
    hours_per_year: float = 8760.0,
) -> Metrics:
    """Compute performance metrics from backtest result.

    Args:
        result: BacktestResult from simulator.
        btc_data: Optional BTC 1h candle DataFrame for benchmark.
        hours_per_year: Hours per year for annualization (default 8760).

    Returns:
        Metrics dataclass.
    """
    curve = np.array(result.equity_curve, dtype=float)

    # Cumulative return
    if len(curve) < 2 or curve[0] == 0:
        return Metrics(
            cumulative_return=0.0,
            cagr=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            trade_count=len(result.trades),
            turnover=0.0,
            benchmark_return=_calc_benchmark(btc_data, result.timestamps),
        )

    cum_return = (curve[-1] / curve[0]) - 1.0

    # CAGR
    n_hours = len(curve) - 1
    years = n_hours / hours_per_year
    if years > 0 and curve[-1] > 0 and curve[0] > 0:
        cagr = (curve[-1] / curve[0]) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0

    # Hourly returns for Sharpe
    returns = np.diff(curve) / curve[:-1]
    returns = returns[np.isfinite(returns)]

    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(hours_per_year)
    else:
        sharpe = 0.0

    # Max drawdown
    peak = np.maximum.accumulate(curve)
    drawdowns = (peak - curve) / peak
    max_dd = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

    # Turnover: total traded value / average portfolio value
    total_traded = sum(
        abs(t.get("quantity", 0) * t.get("price", 0)) for t in result.trades
    )
    avg_equity = float(np.mean(curve))
    turnover = total_traded / avg_equity if avg_equity > 0 else 0.0

    return Metrics(
        cumulative_return=cum_return,
        cagr=cagr,
        sharpe=sharpe,
        max_drawdown=max_dd,
        trade_count=len(result.trades),
        turnover=turnover,
        benchmark_return=_calc_benchmark(btc_data, result.timestamps),
    )


def _calc_benchmark(
    btc_data: pd.DataFrame | None,
    timestamps: list[str],
) -> float | None:
    """Calculate BTC buy & hold return over the same period."""
    if btc_data is None or len(btc_data) == 0 or len(timestamps) < 2:
        return None

    start_ts = timestamps[0]
    end_ts = timestamps[-1]

    # Find closest prices
    start_mask = btc_data["datetime"] <= start_ts
    end_mask = btc_data["datetime"] <= end_ts

    start_rows = btc_data[start_mask]
    end_rows = btc_data[end_mask]

    if len(start_rows) == 0 or len(end_rows) == 0:
        return None

    start_price = float(start_rows.iloc[-1]["close"])
    end_price = float(end_rows.iloc[-1]["close"])

    if start_price == 0:
        return None

    return (end_price / start_price) - 1.0
