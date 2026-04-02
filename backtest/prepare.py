"""Backtest data preparation and engine."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

# Constants
INITIAL_CAPITAL = 10_000_000
TRADING_FEE = 0.0005  # 0.05%
SLIPPAGE_BPS = 2.0  # 2 basis points
LOOKBACK_BARS = 200
BAR_INTERVAL = "1d"

# Default universe (fixed)
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "LINK", "ADA", "DOT", "AVAX"]

# Split dates - revised to ensure each split has RSI<30 trading signals.
# Proposal A: train has bull+bear mix, val includes Nov 2025 bear,
# test is the most recent holdout period.
# BTC RSI<30 days: train=12, val=16, test=7
SPLITS = {
    "train": {"start": "2024-04-01", "end": "2025-06-30"},
    "val": {"start": "2025-07-01", "end": "2026-01-31"},
    "test": {"start": "2026-02-01", "end": "2026-03-22"},
}

# Data directory
DATA_DIR = Path(__file__).resolve().parent / "data"


def lookback_bars_for_interval(interval: str) -> int:
    """Return required history bars for a bar interval."""
    return 200 if interval == "1d" else 500


def annualization_factor(interval: str) -> float:
    """Return Sharpe annualization factor (sqrt of bars per year)."""
    if interval == "1d":
        bars_per_year = 365.0
    elif interval.endswith("m"):
        minutes = max(int(interval[:-1]), 1)
        bars_per_year = (365.0 * 24.0 * 60.0) / minutes
    elif interval.endswith("h"):
        hours = max(int(interval[:-1]), 1)
        bars_per_year = (365.0 * 24.0) / hours
    else:
        bars_per_year = 365.0
    return float(np.sqrt(bars_per_year))


def data_dir_for_interval(interval: str) -> Path:
    """Return data directory for bar interval."""
    return DATA_DIR if interval == "1d" else DATA_DIR / interval


def validate_and_fill(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval == "1d" or df.empty:
        return df

    if interval not in {"1h", "4h"}:
        return df

    freq = interval.lower()
    prepared = df.copy()
    prepared["date"] = pd.to_datetime(prepared["date"])
    prepared = prepared.sort_values("date").drop_duplicates(subset="date", keep="last")
    prepared = prepared.set_index("date")

    full_index = pd.date_range(
        start=prepared.index.min(),
        end=prepared.index.max(),
        freq=freq,
    )
    reindexed = prepared.reindex(full_index)

    max_gap_bars = int(pd.Timedelta("24h") / pd.Timedelta(freq)) - 1
    filled = reindexed.ffill(limit=max_gap_bars)
    filled.index.name = "date"
    result = filled.reset_index()
    result["date"] = result["date"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return result


# Walk-forward cross-validation folds
# Each fold: train period expands, val is next 3 months
# train_start/train_end are documented for context (warmup window);
# cross_validate() evaluates only on the val window.
CV_FOLDS = [
    {
        "train_start": "2024-04-01",
        "train_end": "2025-03-31",
        "val_start": "2025-04-01",
        "val_end": "2025-06-30",
    },
    {
        "train_start": "2024-04-01",
        "train_end": "2025-06-30",
        "val_start": "2025-07-01",
        "val_end": "2025-09-30",
    },
    {
        "train_start": "2024-04-01",
        "train_end": "2025-09-30",
        "val_start": "2025-10-01",
        "val_end": "2025-12-31",
    },
    {
        "train_start": "2024-04-01",
        "train_end": "2025-12-31",
        "val_start": "2026-01-01",
        "val_end": "2026-03-22",
    },
]


@dataclass(frozen=True)
class BarData:
    """Single bar/candle data with symbol and history."""

    symbol: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float
    history: pd.DataFrame = field(
        repr=False
    )  # LOOKBACK_BARS of history including current bar


@dataclass
class Signal:
    """Trading signal from strategy."""

    symbol: str
    action: str  # "buy" or "sell"
    weight: (
        float  # Target portfolio weight (0-1) for buy, fraction to sell (0-1) for sell
    )
    reason: str = ""  # Reason for the signal


@dataclass
class PortfolioState:
    """Current portfolio state."""

    cash: float
    positions: dict[str, float]  # symbol -> quantity
    avg_prices: dict[str, float]  # symbol -> avg entry price
    position_dates: dict[str, str]  # symbol -> entry date
    equity: float = 0.0  # Current portfolio equity
    date: str = ""  # Current date
    trade_log: list[dict[str, Any]] = field(default_factory=list)

    def copy(self) -> "PortfolioState":
        """Create a copy of the state."""
        return PortfolioState(
            cash=self.cash,
            positions=self.positions.copy(),
            avg_prices=self.avg_prices.copy(),
            position_dates=self.position_dates.copy(),
            equity=self.equity,
            date=self.date,
            trade_log=self.trade_log.copy(),
        )


@dataclass
class BacktestResult:
    """Result of a backtest run."""

    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float  # Changed from win_rate for clarity
    profit_factor: float
    avg_holding_days: float
    time_in_market_pct: float = 0.0  # Percentage of days with open positions
    backtest_seconds: float = 0.0  # Runtime measurement
    trade_log: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    equity_dates: list[str] = field(default_factory=list)


@dataclass
class CVResult:
    """Cross-validation result."""

    fold_scores: list[float]
    fold_results: list[BacktestResult]
    fold_indices: list[int]  # Which CV_FOLDS indices were actually evaluated
    mean_score: float
    std_score: float
    min_score: float
    cv_score: float  # Final score with penalties


class Strategy(Protocol):
    """Protocol for strategy implementations."""

    def on_bar(
        self,
        bar_data: dict[str, BarData],
        portfolio: PortfolioState,
    ) -> list[Signal]:
        """Called for each bar to generate signals.

        Args:
            bar_data: Dictionary mapping symbol to BarData with history
            portfolio: Current portfolio state with equity and date

        Returns:
            List of signals to execute
        """
        ...


def _resolve_split_dates(split: str) -> tuple[str, str]:
    """Resolve split name to start and end dates."""
    if split not in SPLITS:
        raise ValueError(f"Unknown split: {split}. Available: {list(SPLITS.keys())}")
    return SPLITS[split]["start"], SPLITS[split]["end"]


def load_data_range(
    start: str,
    end: str,
    bar_interval: str = "1d",
    require_all_symbols: bool = True,
) -> dict[str, pd.DataFrame]:
    """Load backtest data for an arbitrary date range.

    Args:
        start: Start date (YYYY-MM-DD)
        end: End date (YYYY-MM-DD)
        bar_interval: Bar interval to load (e.g. "1d", "60m", "5m")
        require_all_symbols: Fail fast when expected symbol files are missing

    Returns:
        Dictionary mapping symbol to DataFrame with OHLCV data
    """
    data: dict[str, pd.DataFrame] = {}
    interval_dir = data_dir_for_interval(bar_interval)
    missing_symbols: list[str] = []
    for symbol in DEFAULT_SYMBOLS:
        path = interval_dir / f"KRW-{symbol}.parquet"
        if not path.exists():
            missing_symbols.append(symbol)
            continue
        df = pd.read_parquet(path)
        required = ["date", "open", "high", "low", "close", "volume", "value"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Missing columns in {path}: {missing}")
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        df = df.sort_values("date").reset_index(drop=True)
        if len(df) > 0:
            data[symbol] = df

    if require_all_symbols and missing_symbols:
        missing_csv = ", ".join(missing_symbols)
        fetch_symbols = " ".join(missing_symbols)
        raise ValueError(
            "Missing backtest data for symbols: "
            f"{missing_csv}. "
            "Refresh the fixed universe with "
            f"`uv run backtest/fetch_data.py --symbols {fetch_symbols}`."
        )
    return data


def load_data(
    split: str = "val",
    bar_interval: str = "1d",
    require_all_symbols: bool = True,
) -> dict[str, pd.DataFrame]:
    """Load backtest data for the given split.

    Args:
        split: Data split to load ("train", "val", or "test")
        bar_interval: Bar interval to load (e.g. "1d", "60m", "5m")
        require_all_symbols: Fail fast when expected symbol files are missing

    Returns:
        Dictionary mapping symbol to DataFrame with OHLCV data
    """
    start, end = _resolve_split_dates(split)
    return load_data_range(
        start,
        end,
        bar_interval=bar_interval,
        require_all_symbols=require_all_symbols,
    )


def _calc_execution_price(
    bar: BarData, action: str, slippage_bps: float = SLIPPAGE_BPS
) -> float:
    """Calculate execution price with slippage.

    For buys: price moves up (higher price)
    For sells: price moves down (lower price)
    """
    slippage = bar.close * (slippage_bps / 10000)
    if action == "buy":
        return bar.close + slippage
    else:  # sell
        return bar.close - slippage


def _calc_fee(amount: float, fee_rate: float = TRADING_FEE) -> float:
    """Calculate trading fee."""
    return amount * fee_rate


def _calc_buy_quantity(
    cash: float,
    weight: float,
    portfolio_value: float,
    price: float,
) -> float:
    """Calculate quantity to buy for target weight, accounting for fees.

    Args:
        cash: Available cash
        weight: Target portfolio weight (0-1)
        portfolio_value: Total portfolio value
        price: Execution price including slippage

    Returns:
        Quantity to buy that is affordable after fees
    """
    target_value = portfolio_value * weight
    target_qty = target_value / price
    # Calculate max affordable accounting for fees: cost + fee = cost * (1 + fee_rate)
    max_affordable_qty = cash / (price * (1 + TRADING_FEE))
    quantity = min(target_qty, max_affordable_qty)
    return max(0.0, quantity)


def _calc_sell_quantity(
    current_qty: float,
    weight: float,
) -> float:
    """Calculate quantity to sell as fraction of currently held quantity.

    Args:
        current_qty: Current position quantity
        weight: Fraction of position to sell (0-1), where 1.0 means full liquidation

    Returns:
        Quantity to sell
    """
    # weight is the fraction of current position to sell
    # weight=1.0 means sell all, weight=0.25 means sell 25%
    if weight <= 0:
        return 0.0
    return current_qty * min(weight, 1.0)


def _update_avg_price(
    current_qty: float,
    current_avg: float,
    new_qty: float,
    new_price: float,
) -> float:
    """Update average price after a buy."""
    if current_qty + new_qty == 0:
        return 0.0
    total_cost = (current_qty * current_avg) + (new_qty * new_price)
    return total_cost / (current_qty + new_qty)


def _calc_realized_pnl(
    qty: float,
    avg_price: float,
    sell_price: float,
    sell_fee: float,
) -> float:
    """Calculate realized PnL for a sell."""
    return (sell_price * qty - sell_fee) - (avg_price * qty)


def _execute_signal(
    signal: Signal,
    state: PortfolioState,
    bar_data: dict[str, BarData],
    portfolio_value: float,
) -> PortfolioState:
    """Execute a trading signal and return new state."""
    if signal.symbol not in bar_data:
        return state

    bar = bar_data[signal.symbol]
    new_state = state.copy()

    if signal.action == "buy":
        price = _calc_execution_price(bar, "buy")
        quantity = _calc_buy_quantity(state.cash, signal.weight, portfolio_value, price)

        if quantity > 0:
            cost = quantity * price
            fee = _calc_fee(cost)
            total_cost = cost + fee

            if total_cost <= state.cash:
                # Update position
                current_qty = state.positions.get(signal.symbol, 0)
                current_avg = state.avg_prices.get(signal.symbol, 0)
                effective_buy_price = total_cost / quantity

                new_state.cash = state.cash - total_cost
                new_state.positions[signal.symbol] = current_qty + quantity
                new_state.avg_prices[signal.symbol] = _update_avg_price(
                    current_qty, current_avg, quantity, effective_buy_price
                )
                new_state.position_dates[signal.symbol] = bar.date

                # Log trade
                new_state.trade_log.append(
                    {
                        "date": bar.date,
                        "symbol": signal.symbol,
                        "action": "buy",
                        "quantity": quantity,
                        "price": price,
                        "fee": fee,
                        "reason": signal.reason,
                    }
                )

    elif signal.action == "sell":
        current_qty = state.positions.get(signal.symbol, 0)
        if current_qty > 0:
            price = _calc_execution_price(bar, "sell")
            quantity = _calc_sell_quantity(current_qty, signal.weight)

            if quantity > 0:
                proceeds = quantity * price
                fee = _calc_fee(proceeds)
                net_proceeds = proceeds - fee
                avg_price = state.avg_prices.get(signal.symbol, 0)
                realized_pnl = _calc_realized_pnl(quantity, avg_price, price, fee)

                # Update position
                new_state.cash = state.cash + net_proceeds
                new_state.positions[signal.symbol] = current_qty - quantity

                if new_state.positions[signal.symbol] <= 0:
                    del new_state.positions[signal.symbol]
                    del new_state.avg_prices[signal.symbol]
                    if signal.symbol in new_state.position_dates:
                        del new_state.position_dates[signal.symbol]

                # Log trade
                new_state.trade_log.append(
                    {
                        "date": bar.date,
                        "symbol": signal.symbol,
                        "action": "sell",
                        "quantity": quantity,
                        "price": price,
                        "fee": fee,
                        "realized_pnl": realized_pnl,
                        "reason": signal.reason,
                    }
                )

    return new_state


def run_backtest(
    data: dict[str, pd.DataFrame],
    strategy: Strategy,
    initial_capital: float = INITIAL_CAPITAL,
    bar_interval: str = "1d",
) -> BacktestResult:
    """Run backtest with given data and strategy.

    Args:
        data: Dictionary mapping symbol to DataFrame
        strategy: Strategy instance implementing on_bar protocol
        initial_capital: Starting capital
        bar_interval: Bar interval used for history depth and data source

    Returns:
        BacktestResult with metrics and trade log
    """
    import time

    start_time = time.time()

    # Build unified date sequence from the split window only.
    all_dates = set()
    for df in data.values():
        all_dates.update(df["date"].tolist())
    dates = sorted(all_dates)

    if not dates:
        return BacktestResult(
            total_return_pct=0.0,
            sharpe=0.0,
            max_drawdown_pct=0.0,
            num_trades=0,
            win_rate_pct=0.0,
            profit_factor=0.0,
            avg_holding_days=0.0,
            time_in_market_pct=0.0,
            backtest_seconds=0.0,
            trade_log=[],
            equity_curve=[initial_capital],
            equity_dates=[],
        )

    # Pre-index data by symbol for efficient lookup.
    # Prefer the on-disk source when available so history can include
    # pre-split warmup bars even if `load_data()` filtered them out.
    interval_dir = data_dir_for_interval(bar_interval)
    lookback_bars = lookback_bars_for_interval(bar_interval)
    indexed_data: dict[str, pd.DataFrame] = {}
    for symbol, df in data.items():
        full_path = interval_dir / f"KRW-{symbol}.parquet"
        source_df = pd.read_parquet(full_path) if full_path.exists() else df
        indexed_data[symbol] = source_df.set_index("date").sort_index()

    # Initialize state
    state = PortfolioState(
        cash=initial_capital,
        positions={},
        avg_prices={},
        position_dates={},
        trade_log=[],
    )

    equity_curve = [initial_capital]
    equity_dates = [dates[0]]
    days_in_market = 0

    # Iterate through dates
    for date in dates:
        # Build bar_data for this date with history
        bar_data: dict[str, BarData] = {}
        for symbol, df in indexed_data.items():
            if date in df.index:
                row = df.loc[date]
                # Get history up to and including current date (lookback_bars rows)
                loc = df.index.get_loc(date)
                if isinstance(loc, slice):
                    idx = loc.stop - 1
                elif isinstance(loc, np.ndarray):
                    if loc.dtype == bool:
                        matches = np.flatnonzero(loc)
                        if len(matches) == 0:
                            continue
                        idx = int(matches[-1])
                    else:
                        if len(loc) == 0:
                            continue
                        idx = int(loc[-1])
                else:
                    idx = int(loc)
                start_idx = max(0, idx - lookback_bars + 1)
                history = df.iloc[start_idx : idx + 1].copy()

                bar_data[symbol] = BarData(
                    symbol=symbol,
                    date=date,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    value=row["value"],
                    history=history,
                )

        # Calculate current portfolio value
        portfolio_value = state.cash
        for symbol, qty in state.positions.items():
            if symbol in bar_data:
                portfolio_value += qty * bar_data[symbol].close

        # Populate portfolio state with equity and date
        state.equity = portfolio_value
        state.date = date

        # Get signals from strategy (new two-argument interface)
        signals = strategy.on_bar(bar_data, state)

        # Execute signals
        for signal in signals:
            state = _execute_signal(signal, state, bar_data, portfolio_value)

        # Recalculate equity after execution
        equity = state.cash
        for symbol, qty in state.positions.items():
            if symbol in bar_data:
                equity += qty * bar_data[symbol].close
        equity_curve.append(equity)
        equity_dates.append(date)

        # Track days in market (after signal execution)
        if state.positions:
            days_in_market += 1

    # Calculate metrics
    elapsed = time.time() - start_time
    time_in_market_pct = days_in_market / len(dates) * 100.0 if dates else 0.0
    return _build_result(
        state,
        equity_curve,
        elapsed,
        bar_interval=bar_interval,
        equity_dates=equity_dates,
        time_in_market_pct=time_in_market_pct,
    )


def _build_result(
    state: PortfolioState,
    equity_curve: list[float],
    backtest_seconds: float = 0.0,
    bar_interval: str = "1d",
    equity_dates: list[str] | None = None,
    time_in_market_pct: float = 0.0,
) -> BacktestResult:
    """Build BacktestResult from final state."""
    total_return_pct = _calc_total_return(equity_curve)
    max_drawdown_pct = _calc_max_drawdown(equity_curve)

    # Calculate daily returns for sharpe
    if len(equity_curve) >= 2:
        daily_returns = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
        ]
        sharpe = _calc_sharpe(
            daily_returns,
            annualize_factor=annualization_factor(bar_interval),
        )
    else:
        sharpe = 0.0

    num_trades = len(state.trade_log)
    win_rate_pct, profit_factor, avg_holding_days = _calc_trade_metrics(state.trade_log)

    return BacktestResult(
        total_return_pct=total_return_pct,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        num_trades=num_trades,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        avg_holding_days=avg_holding_days,
        time_in_market_pct=time_in_market_pct,
        backtest_seconds=backtest_seconds,
        trade_log=state.trade_log,
        equity_curve=equity_curve,
        equity_dates=equity_dates or [],
    )


def _calc_total_return(equity_curve: list[float]) -> float:
    """Calculate total return percentage."""
    if not equity_curve or equity_curve[0] == 0:
        return 0.0
    return (equity_curve[-1] - equity_curve[0]) / equity_curve[0] * 100


def _calc_sharpe(
    returns: list[float],
    risk_free_rate: float = 0.0,
    annualize_factor: float | None = None,
) -> float:
    """Calculate annualized Sharpe ratio."""
    if not returns:
        return 0.0

    arr = np.array(returns)
    mean_return = np.mean(arr) - risk_free_rate
    std_return = np.std(arr, ddof=1)

    if std_return == 0 or np.isnan(std_return):
        return 0.0

    factor = annualize_factor if annualize_factor is not None else np.sqrt(365)
    sharpe = (mean_return / std_return) * factor
    return float(sharpe)


def _calc_max_drawdown(equity_curve: list[float]) -> float:
    """Calculate maximum drawdown percentage."""
    if not equity_curve:
        return 0.0

    peak = equity_curve[0]
    max_dd = 0.0

    for equity in equity_curve[1:]:
        if equity > peak:
            peak = equity
        drawdown = (peak - equity) / peak * 100
        max_dd = max(max_dd, drawdown)

    return max_dd


def _calc_trade_metrics(trade_log: list[dict[str, Any]]) -> tuple[float, float, float]:
    """Calculate trade metrics: win_rate_pct, profit_factor, avg_holding_days."""
    if not trade_log:
        return 0.0, 0.0, 0.0

    # Match buy/sell pairs for PnL
    trades_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for trade in trade_log:
        symbol = trade["symbol"]
        if symbol not in trades_by_symbol:
            trades_by_symbol[symbol] = []
        trades_by_symbol[symbol].append(trade)

    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    holding_days_list: list[float] = []

    for _symbol, trades in trades_by_symbol.items():
        # Simple round-trip matching
        position = 0.0
        entry_date = None

        for trade in trades:
            if trade["action"] == "buy":
                position += trade["quantity"]
                if entry_date is None:
                    entry_date = trade["date"]
            elif trade["action"] == "sell":
                if position > 0 and "realized_pnl" in trade:
                    pnl = trade["realized_pnl"]
                    if pnl > 0:
                        wins += 1
                        gross_profit += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)

                    # Calculate holding days (simplified)
                    if entry_date:
                        try:
                            from datetime import datetime

                            entry = datetime.strptime(entry_date, "%Y-%m-%d")
                            exit_date = datetime.strptime(trade["date"], "%Y-%m-%d")
                            days = (exit_date - entry).days
                            holding_days_list.append(max(0, days))
                        except (ValueError, TypeError):
                            pass

                position -= trade["quantity"]
                if position <= 0:
                    position = 0
                    entry_date = None

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else gross_profit
    avg_holding_days = float(np.mean(holding_days_list)) if holding_days_list else 0.0

    return win_rate, profit_factor, avg_holding_days


def compute_score(result: BacktestResult) -> float:
    """Compute composite score with anti-gaming penalties.

    Base score is Sharpe ratio. Progressive penalties are applied to prevent
    score hacking via low-exposure, low-return, or insufficient-trade strategies:

    1. Drawdown penalty: Applied when max_drawdown_pct > 20%
       penalty = (max_drawdown_pct - 20) * 0.1

    2. Trade count penalty: Applied when round_trips < 15
       penalty = (15 - round_trips) * 0.2
       where round_trips = num_trades // 2

    3. Time-in-market penalty: Applied when time_in_market_pct < 20%
       penalty = (20.0 - time_in_market_pct) * 0.1

    4. Total return penalty: Applied when total_return_pct < 2%
       penalty = (2.0 - total_return_pct) * 0.5

    5. Holding period penalty: Applied when avg_holding_days < 1.5
       penalty = 0.5 flat
    """
    score = result.sharpe

    # Drawdown penalty: applied when drawdown exceeds 20%
    if result.max_drawdown_pct > 20:
        score -= (result.max_drawdown_pct - 20) * 0.1

    # Trade count penalty: based on round trips (each round trip = buy + sell)
    round_trips = result.num_trades // 2
    if round_trips < 15:
        score -= (15 - round_trips) * 0.2

    # Time-in-market penalty: applied when exposure is too low
    if result.time_in_market_pct < 20.0:
        score -= (20.0 - result.time_in_market_pct) * 0.1

    # Total return penalty: applied when returns are too low
    if result.total_return_pct < 2.0:
        score -= (2.0 - result.total_return_pct) * 0.5

    # Holding period penalty: applied when holding periods are too short
    if result.avg_holding_days < 1.5:
        score -= 0.5

    return score


def cross_validate(
    strategy_class: type,
    folds: list[dict[str, str]] | None = None,
    initial_capital: float = INITIAL_CAPITAL,
    bar_interval: str = BAR_INTERVAL,
) -> CVResult:
    """Run walk-forward cross-validation.

    Each fold gets a fresh strategy instance to prevent state leakage.
    Folds with no data are skipped (fold_indices tracks which were evaluated).

    Args:
        strategy_class: Strategy class (not instance — instantiated per fold).
                        Must be constructible with no arguments.
        folds: List of fold dicts with train_start/train_end/val_start/val_end.
               Defaults to CV_FOLDS.
        initial_capital: Starting capital per fold

    Returns:
        CVResult with per-fold and aggregate scores
    """
    if folds is None:
        folds = CV_FOLDS

    fold_scores: list[float] = []
    fold_results: list[BacktestResult] = []
    fold_indices: list[int] = []

    for i, fold in enumerate(folds):
        val_data = load_data_range(
            fold["val_start"],
            fold["val_end"],
            bar_interval=bar_interval,
        )
        if not val_data:
            continue

        strat = strategy_class()
        result = run_backtest(
            val_data, strat, initial_capital, bar_interval=bar_interval
        )
        score = compute_score(result)

        fold_scores.append(score)
        fold_results.append(result)
        fold_indices.append(i)

    if not fold_scores:
        return CVResult(
            fold_scores=[],
            fold_results=[],
            fold_indices=[],
            mean_score=-999.0,
            std_score=0.0,
            min_score=-999.0,
            cv_score=-999.0,
        )

    mean_score = float(np.mean(fold_scores))
    std_score = float(np.std(fold_scores))
    min_score = float(np.min(fold_scores))

    # CV score: mean - std penalty - catastrophic fold penalty
    cv_score = mean_score - 0.5 * std_score
    catastrophic_folds = sum(1 for s in fold_scores if s < -2.0)
    cv_score -= catastrophic_folds * 1.0

    return CVResult(
        fold_scores=fold_scores,
        fold_results=fold_results,
        fold_indices=fold_indices,
        mean_score=mean_score,
        std_score=std_score,
        min_score=min_score,
        cv_score=cv_score,
    )
