"""Rebalancing portfolio simulator for RSI strategy."""

from dataclasses import dataclass, field

import pandas as pd

from .config import BacktestConfig
from .strategy import select_coins
from .universe import select_universe


@dataclass
class Portfolio:
    """Mutable portfolio state."""

    cash: float
    positions: dict[str, float] = field(default_factory=dict)  # market -> quantity

    def equity(self, prices: dict[str, float]) -> float:
        """Total portfolio value at current prices."""
        pos_value = sum(
            qty * prices.get(market, 0) for market, qty in self.positions.items()
        )
        return self.cash + pos_value


@dataclass
class BacktestResult:
    """Output of a backtest run."""

    equity_curve: list[float]
    timestamps: list[str]
    trades: list[dict]
    rebalance_count: int
    config: BacktestConfig


def _get_prices_at(all_data: dict[str, pd.DataFrame], timestamp: str) -> dict[str, float]:
    """Get close prices for all markets at a specific timestamp."""
    prices: dict[str, float] = {}
    for market, df in all_data.items():
        mask = df["datetime"] == timestamp
        rows = df[mask]
        if len(rows) > 0:
            prices[market] = float(rows.iloc[0]["close"])
    return prices


def _execute_rebalance(
    portfolio: Portfolio,
    target_markets: list[str],
    prices: dict[str, float],
    config: BacktestConfig,
    timestamp: str,
) -> list[dict]:
    """Rebalance portfolio to equal-weight target markets.

    Returns list of trade records.
    """
    trades: list[dict] = []
    total_equity = portfolio.equity(prices)

    if not target_markets or total_equity <= 0:
        return trades

    target_weight = 1.0 / len(target_markets)
    target_value_per_coin = total_equity * target_weight
    slippage_mult = config.slippage_bps / 10_000

    # Phase 1: Sell positions not in target (or over-weight)
    for market in list(portfolio.positions.keys()):
        if market not in target_markets:
            qty = portfolio.positions[market]
            if qty > 0 and market in prices:
                sell_price = prices[market] * (1 - slippage_mult)
                proceeds = qty * sell_price
                fee = proceeds * config.fee_rate
                portfolio.cash += proceeds - fee
                trades.append({
                    "datetime": timestamp,
                    "market": market,
                    "action": "sell",
                    "quantity": qty,
                    "price": sell_price,
                    "fee": fee,
                })
            portfolio.positions.pop(market, None)

    # Phase 2: Rebalance existing + buy new
    # Recalculate equity after sells
    total_equity = portfolio.equity(prices)
    target_value_per_coin = total_equity * target_weight

    for market in target_markets:
        if market not in prices:
            continue

        price = prices[market]
        current_qty = portfolio.positions.get(market, 0)
        current_value = current_qty * price
        diff_value = target_value_per_coin - current_value

        if abs(diff_value) < total_equity * 0.01:
            # Skip tiny rebalances (< 1% of portfolio)
            continue

        if diff_value > 0:
            # Buy
            buy_price = price * (1 + slippage_mult)
            max_buy_value = portfolio.cash / (1 + config.fee_rate)
            buy_value = min(diff_value, max_buy_value)
            if buy_value <= 0:
                continue
            qty = buy_value / buy_price
            cost = qty * buy_price
            fee = cost * config.fee_rate
            portfolio.cash -= cost + fee
            portfolio.positions[market] = current_qty + qty
            trades.append({
                "datetime": timestamp,
                "market": market,
                "action": "buy",
                "quantity": qty,
                "price": buy_price,
                "fee": fee,
            })
        elif diff_value < 0:
            # Sell excess
            sell_price = price * (1 - slippage_mult)
            sell_qty = min(abs(diff_value) / price, current_qty)
            if sell_qty <= 0:
                continue
            proceeds = sell_qty * sell_price
            fee = proceeds * config.fee_rate
            portfolio.cash += proceeds - fee
            portfolio.positions[market] = current_qty - sell_qty
            if portfolio.positions[market] <= 0:
                portfolio.positions.pop(market, None)
            trades.append({
                "datetime": timestamp,
                "market": market,
                "action": "sell",
                "quantity": sell_qty,
                "price": sell_price,
                "fee": fee,
            })

    return trades


def run_backtest(
    all_data: dict[str, pd.DataFrame],
    config: BacktestConfig,
) -> BacktestResult:
    """Run the full rebalancing backtest.

    Args:
        all_data: Dict mapping market code to 1h candle DataFrame.
        config: Backtest configuration.

    Returns:
        BacktestResult with equity curve, trades, and metadata.
    """
    if not all_data:
        return BacktestResult(
            equity_curve=[config.initial_capital],
            timestamps=[config.start + "T00:00:00"],
            trades=[],
            rebalance_count=0,
            config=config,
        )

    # Build unified sorted timeline
    all_timestamps: set[str] = set()
    for df in all_data.values():
        start_filter = f"{config.start}T00:00:00"
        end_filter = f"{config.end}T23:59:59"
        mask = (df["datetime"] >= start_filter) & (df["datetime"] <= end_filter)
        all_timestamps.update(df[mask]["datetime"].tolist())

    timestamps = sorted(all_timestamps)
    if not timestamps:
        return BacktestResult(
            equity_curve=[config.initial_capital],
            timestamps=[config.start + "T00:00:00"],
            trades=[],
            rebalance_count=0,
            config=config,
        )

    portfolio = Portfolio(cash=config.initial_capital)
    equity_curve: list[float] = []
    equity_timestamps: list[str] = []
    all_trades: list[dict] = []
    rebalance_count = 0
    bars_since_rebalance = config.rebalance_hours  # Force rebalance on first bar

    for ts in timestamps:
        prices = _get_prices_at(all_data, ts)

        # Check if it's time to rebalance
        if bars_since_rebalance >= config.rebalance_hours:
            # Select universe
            universe = select_universe(all_data, ts, config.top_n, window=24)

            # Select coins by RSI
            selected = select_coins(universe, all_data, ts, config)

            # Execute rebalance
            trades = _execute_rebalance(portfolio, selected, prices, config, ts)
            all_trades.extend(trades)
            rebalance_count += 1
            bars_since_rebalance = 0

        bars_since_rebalance += 1

        # Record equity
        equity = portfolio.equity(prices)
        equity_curve.append(equity)
        equity_timestamps.append(ts)

    return BacktestResult(
        equity_curve=equity_curve,
        timestamps=equity_timestamps,
        trades=all_trades,
        rebalance_count=rebalance_count,
        config=config,
    )
