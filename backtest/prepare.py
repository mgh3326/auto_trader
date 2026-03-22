"""Backtest data preparation and engine."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

# Constants
INITIAL_CAPITAL = 100_000.0
TRADING_FEE = 0.0005  # 0.05%
SLIPPAGE_BPS = 10  # 10 basis points (0.1%)
LOOKBACK_BARS = 20
BAR_INTERVAL = "1d"

# Default universe (fixed)
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

# Split dates
SPLITS = {
    "train": {"start": "2023-01-01", "end": "2024-06-30"},
    "val": {"start": "2024-07-01", "end": "2024-12-31"},
    "test": {"start": "2025-01-01", "end": "2025-12-31"},
}

# Data directory
DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class BarData:
    """Single bar/candle data."""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass
class Signal:
    """Trading signal from strategy."""
    symbol: str
    action: str  # "buy" or "sell"
    target_weight: float  # Target portfolio weight (0-1)


@dataclass
class PortfolioState:
    """Current portfolio state."""
    cash: float
    positions: dict[str, float]  # symbol -> quantity
    avg_prices: dict[str, float]  # symbol -> avg entry price
    position_dates: dict[str, str]  # symbol -> entry date
    trade_log: list[dict[str, Any]] = field(default_factory=list)

    def copy(self) -> "PortfolioState":
        """Create a copy of the state."""
        return PortfolioState(
            cash=self.cash,
            positions=self.positions.copy(),
            avg_prices=self.avg_prices.copy(),
            position_dates=self.position_dates.copy(),
            trade_log=self.trade_log.copy(),
        )


@dataclass
class BacktestResult:
    """Result of a backtest run."""
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    num_trades: int
    win_rate: float
    profit_factor: float
    avg_holding_days: float
    trade_log: list[dict[str, Any]]
    equity_curve: list[float]


class Strategy(Protocol):
    """Protocol for strategy implementations."""

    def on_bar(
        self,
        date: str,
        bar_data: dict[str, BarData],
        portfolio: PortfolioState,
        bar_index: int,
    ) -> list[Signal]:
        """Called for each bar to generate signals."""
        ...


def _resolve_split_dates(split: str) -> tuple[str, str]:
    """Resolve split name to start and end dates."""
    if split not in SPLITS:
        raise ValueError(f"Unknown split: {split}. Available: {list(SPLITS.keys())}")
    return SPLITS[split]["start"], SPLITS[split]["end"]


def load_data(split: str = "val") -> dict[str, pd.DataFrame]:
    """Load backtest data for the given split.

    Args:
        split: Data split to load ("train", "val", or "test")

    Returns:
        Dictionary mapping symbol to DataFrame with OHLCV data
    """
    start, end = _resolve_split_dates(split)
    data: dict[str, pd.DataFrame] = {}

    for symbol in DEFAULT_SYMBOLS:
        path = DATA_DIR / f"KRW-{symbol}.parquet"
        if not path.exists():
            continue

        df = pd.read_parquet(path)

        # Validate required columns
        required = ["date", "open", "high", "low", "close", "volume", "value"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise ValueError(f"Missing columns in {path}: {missing}")

        # Filter by date range
        df = df[(df["date"] >= start) & (df["date"] <= end)]

        # Sort by date ascending
        df = df.sort_values("date").reset_index(drop=True)

        # Only keep non-empty frames
        if len(df) > 0:
            data[symbol] = df

    return data


def _calc_execution_price(bar: BarData, action: str, slippage_bps: int = SLIPPAGE_BPS) -> float:
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
    target_weight: float,
    portfolio_value: float,
    price: float,
) -> float:
    """Calculate quantity to buy for target weight."""
    target_value = portfolio_value * target_weight
    max_affordable = cash / price
    quantity = min(target_value / price, max_affordable)
    return max(0.0, quantity)


def _calc_sell_quantity(
    current_qty: float,
    target_weight: float,
    portfolio_value: float,
    price: float,
) -> float:
    """Calculate quantity to sell for target weight."""
    current_value = current_qty * price
    current_weight = current_value / portfolio_value if portfolio_value > 0 else 0

    # If selling all
    if target_weight <= 0:
        return current_qty

    # If reducing position
    if target_weight < current_weight:
        target_value = portfolio_value * target_weight
        target_qty = target_value / price
        return max(0.0, current_qty - target_qty)

    return 0.0


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
) -> float:
    """Calculate realized PnL for a sell."""
    return (sell_price - avg_price) * qty


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
        quantity = _calc_buy_quantity(
            state.cash, signal.target_weight, portfolio_value, price
        )

        if quantity > 0:
            cost = quantity * price
            fee = _calc_fee(cost)
            total_cost = cost + fee

            if total_cost <= state.cash:
                # Update position
                current_qty = state.positions.get(signal.symbol, 0)
                current_avg = state.avg_prices.get(signal.symbol, 0)

                new_state.cash = state.cash - total_cost
                new_state.positions[signal.symbol] = current_qty + quantity
                new_state.avg_prices[signal.symbol] = _update_avg_price(
                    current_qty, current_avg, quantity, price
                )
                new_state.position_dates[signal.symbol] = bar.date

                # Log trade
                new_state.trade_log.append({
                    "date": bar.date,
                    "symbol": signal.symbol,
                    "action": "buy",
                    "quantity": quantity,
                    "price": price,
                    "fee": fee,
                })

    elif signal.action == "sell":
        current_qty = state.positions.get(signal.symbol, 0)
        if current_qty > 0:
            price = _calc_execution_price(bar, "sell")
            quantity = _calc_sell_quantity(
                current_qty, signal.target_weight, portfolio_value, price
            )

            if quantity > 0:
                proceeds = quantity * price
                fee = _calc_fee(proceeds)
                net_proceeds = proceeds - fee
                avg_price = state.avg_prices.get(signal.symbol, 0)
                realized_pnl = _calc_realized_pnl(quantity, avg_price, price)

                # Update position
                new_state.cash = state.cash + net_proceeds
                new_state.positions[signal.symbol] = current_qty - quantity

                if new_state.positions[signal.symbol] <= 0:
                    del new_state.positions[signal.symbol]
                    del new_state.avg_prices[signal.symbol]
                    if signal.symbol in new_state.position_dates:
                        del new_state.position_dates[signal.symbol]

                # Log trade
                new_state.trade_log.append({
                    "date": bar.date,
                    "symbol": signal.symbol,
                    "action": "sell",
                    "quantity": quantity,
                    "price": price,
                    "fee": fee,
                    "realized_pnl": realized_pnl,
                })

    return new_state


def run_backtest(
    data: dict[str, pd.DataFrame],
    strategy: Strategy,
    initial_capital: float = INITIAL_CAPITAL,
) -> BacktestResult:
    """Run backtest with given data and strategy.

    Args:
        data: Dictionary mapping symbol to DataFrame
        strategy: Strategy instance implementing on_bar protocol
        initial_capital: Starting capital

    Returns:
        BacktestResult with metrics and trade log
    """
    # Build unified date sequence
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
            win_rate=0.0,
            profit_factor=0.0,
            avg_holding_days=0.0,
            trade_log=[],
            equity_curve=[initial_capital],
        )

    # Initialize state
    state = PortfolioState(
        cash=initial_capital,
        positions={},
        avg_prices={},
        position_dates={},
        trade_log=[],
    )

    equity_curve = [initial_capital]

    # Iterate through dates
    for i, date in enumerate(dates):
        # Build bar_data for this date
        bar_data: dict[str, BarData] = {}
        for symbol, df in data.items():
            row = df[df["date"] == date]
            if not row.empty:
                bar_data[symbol] = BarData(
                    date=date,
                    open=row["open"].iloc[0],
                    high=row["high"].iloc[0],
                    low=row["low"].iloc[0],
                    close=row["close"].iloc[0],
                    volume=row["volume"].iloc[0],
                    value=row["value"].iloc[0],
                )

        # Calculate current portfolio value
        portfolio_value = state.cash
        for symbol, qty in state.positions.items():
            if symbol in bar_data:
                portfolio_value += qty * bar_data[symbol].close

        # Get signals from strategy
        signals = strategy.on_bar(date, bar_data, state, i)

        # Execute signals
        for signal in signals:
            state = _execute_signal(signal, state, bar_data, portfolio_value)

        # Recalculate equity after execution
        equity = state.cash
        for symbol, qty in state.positions.items():
            if symbol in bar_data:
                equity += qty * bar_data[symbol].close
        equity_curve.append(equity)

    # Calculate metrics
    return _build_result(state, equity_curve)


def _build_result(state: PortfolioState, equity_curve: list[float]) -> BacktestResult:
    """Build BacktestResult from final state."""
    total_return_pct = _calc_total_return(equity_curve)
    max_drawdown_pct = _calc_max_drawdown(equity_curve)

    # Calculate daily returns for sharpe
    if len(equity_curve) >= 2:
        daily_returns = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
        ]
        sharpe = _calc_sharpe(daily_returns)
    else:
        sharpe = 0.0

    num_trades = len(state.trade_log)
    win_rate, profit_factor, avg_holding_days = _calc_trade_metrics(state.trade_log)

    return BacktestResult(
        total_return_pct=total_return_pct,
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
        num_trades=num_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_holding_days=avg_holding_days,
        trade_log=state.trade_log,
        equity_curve=equity_curve,
    )


def _calc_total_return(equity_curve: list[float]) -> float:
    """Calculate total return percentage."""
    if not equity_curve or equity_curve[0] == 0:
        return 0.0
    return (equity_curve[-1] - equity_curve[0]) / equity_curve[0] * 100


def _calc_sharpe(returns: list[float], risk_free_rate: float = 0.0) -> float:
    """Calculate annualized Sharpe ratio."""
    if not returns:
        return 0.0

    arr = np.array(returns)
    mean_return = np.mean(arr) - risk_free_rate
    std_return = np.std(arr, ddof=1)

    if std_return == 0 or np.isnan(std_return):
        return 0.0

    # Annualize (assuming daily returns)
    sharpe = (mean_return / std_return) * np.sqrt(365)
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
    """Calculate trade metrics: win_rate, profit_factor, avg_holding_days."""
    if not trade_log:
        return 0.0, 0.0, 0.0

    # Match buy/sell pairs for PnL
    trades_by_symbol: dict[str, list[dict]] = {}
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
    avg_holding_days = np.mean(holding_days_list) if holding_days_list else 0.0

    return win_rate, profit_factor, avg_holding_days


def compute_score(result: BacktestResult) -> float:
    """Compute composite score with penalty for low trade count.

    Formula prioritizes return and risk-adjusted metrics while
    penalizing strategies with insufficient trading activity.
    """
    # Base score components
    return_component = max(0, result.total_return_pct)
    risk_adjusted = max(0, result.sharpe * 10)  # Scale sharpe to similar magnitude

    # Drawdown penalty (less negative = better)
    dd_penalty = max(0, abs(result.max_drawdown_pct))

    # Base score
    score = return_component + risk_adjusted - (dd_penalty * 0.5)

    # Penalty for low trade count (insufficient evidence)
    if result.num_trades < 10:
        penalty_factor = result.num_trades / 10
        score = score * penalty_factor

    return score
