"""Backtest configuration."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestConfig:
    """All parameters for a single backtest run."""

    start: str  # YYYY-MM-DD
    end: str  # YYYY-MM-DD
    top_n: int = 30
    pick_k: int = 5
    max_rsi: float = 45.0
    rsi_period: int = 14
    rebalance_hours: int = 24
    initial_capital: float = 10_000_000
    fee_rate: float = 0.0005  # 0.05%
    slippage_bps: float = 2.0
