"""Backtest strategy implementation."""

import numpy as np
import prepare

# RSI Strategy Constants
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MAX_POSITIONS = 5
POSITION_SIZE = 0.15
HOLDING_DAYS = 7


def _calc_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> float | None:
    """Calculate RSI using Wilder's smoothing method.

    Args:
        closes: Array of closing prices
        period: RSI period (default 14)

    Returns:
        RSI value (0-100) or None if insufficient data
    """
    if len(closes) < period + 1:
        return None

    # Calculate price changes
    deltas = np.diff(closes)

    # Separate gains and losses
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    # Calculate initial averages
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    # Apply Wilder's smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    rs = avg_gain / avg_loss if avg_loss > 0 else float('inf')
    rsi = 100 - (100 / (1 + rs))

    return float(rsi)


class Strategy:
    """RSI-based backtest strategy.

    Buy rules:
    - RSI below oversold threshold (30)
    - Not already holding the symbol
    - Below max positions limit

    Sell rules:
    - RSI above overbought threshold (70): full sell
    - Holding period exceeded (7 days) AND profitable: full sell
    """

    def __init__(self) -> None:
        """Initialize strategy."""
        pass

    def _get_rsi_from_history(self, bar: prepare.BarData) -> float | None:
        """Get RSI from bar's history."""
        if len(bar.history) < RSI_PERIOD + 1:
            return None
        closes = bar.history["close"].values
        return _calc_rsi(closes, RSI_PERIOD)

    def _count_holding_days(self, entry_date: str, current_date: str) -> int:
        """Calculate holding period in days."""
        from datetime import datetime
        try:
            entry = datetime.strptime(entry_date, "%Y-%m-%d")
            current = datetime.strptime(current_date, "%Y-%m-%d")
            return (current - entry).days
        except (ValueError, TypeError):
            return 0

    def on_bar(
        self,
        bar_data: dict[str, prepare.BarData],
        portfolio: prepare.PortfolioState,
    ) -> list[prepare.Signal]:
        """Generate trading signals for the current bar.

        Args:
            bar_data: Dictionary of symbol -> BarData with history
            portfolio: Current portfolio state with equity and date

        Returns:
            List of Signal objects to execute
        """
        signals: list[prepare.Signal] = []
        current_positions = set(portfolio.positions.keys())

        for symbol, bar in bar_data.items():
            # Get RSI from engine-provided history
            rsi = self._get_rsi_from_history(bar)
            if rsi is None:
                continue

            is_held = symbol in current_positions

            # Sell logic
            if is_held:
                # Sell on overbought
                if rsi >= RSI_OVERBOUGHT:
                    signals.append(prepare.Signal(
                        symbol=symbol,
                        action="sell",
                        weight=1.0,  # Full sell
                        reason=f"RSI overbought ({rsi:.1f})",
                    ))
                    continue

                # Sell on holding period exceeded (if profitable)
                entry_date = portfolio.position_dates.get(symbol)
                avg_price = portfolio.avg_prices.get(symbol, 0)
                if entry_date and bar.close > avg_price:
                    holding_days = self._count_holding_days(entry_date, portfolio.date)
                    if holding_days >= HOLDING_DAYS:
                        signals.append(prepare.Signal(
                            symbol=symbol,
                            action="sell",
                            weight=1.0,  # Full sell
                            reason=f"Holding period exceeded ({holding_days} days)",
                        ))
                        continue

            # Buy logic
            if not is_held and len(current_positions) < MAX_POSITIONS:
                # Buy on oversold
                if rsi <= RSI_OVERSOLD:
                    signals.append(prepare.Signal(
                        symbol=symbol,
                        action="buy",
                        weight=POSITION_SIZE,
                        reason=f"RSI oversold ({rsi:.1f})",
                    ))
                    current_positions.add(symbol)

        return signals
