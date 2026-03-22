"""Backtest strategy implementation."""

import numpy as np
import prepare

# RSI Strategy Constants
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MAX_POSITIONS = 5
POSITION_SIZE = 0.15
HOLDING_DAYS = 14
STOP_LOSS_PCT = 0.12  # 12% stop-loss


def _calc_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> float | None:
    """Calculate RSI using Wilder's smoothing method."""
    if len(closes) < period + 1:
        return None

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    rs = avg_gain / avg_loss if avg_loss > 0 else float('inf')
    rsi = 100 - (100 / (1 + rs))
    return float(rsi)


class Strategy:
    """RSI strategy with stop-loss and longer holding period."""

    def __init__(self) -> None:
        pass

    def _get_rsi_from_history(self, bar: prepare.BarData) -> float | None:
        if len(bar.history) < RSI_PERIOD + 1:
            return None
        closes = bar.history["close"].values
        return _calc_rsi(closes, RSI_PERIOD)

    def _count_holding_days(self, entry_date: str, current_date: str) -> int:
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
        signals: list[prepare.Signal] = []
        current_positions = set(portfolio.positions.keys())

        for symbol, bar in bar_data.items():
            rsi = self._get_rsi_from_history(bar)
            if rsi is None:
                continue

            is_held = symbol in current_positions

            # Sell logic
            if is_held:
                avg_price = portfolio.avg_prices.get(symbol, 0)

                # Stop-loss
                if avg_price > 0 and bar.close < avg_price * (1 - STOP_LOSS_PCT):
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=f"Stop-loss ({(bar.close/avg_price - 1)*100:.1f}%)",
                    ))
                    current_positions.discard(symbol)
                    continue

                # Sell on overbought
                if rsi >= RSI_OVERBOUGHT:
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=f"RSI overbought ({rsi:.1f})",
                    ))
                    current_positions.discard(symbol)
                    continue

                # Sell on holding period exceeded (if profitable)
                entry_date = portfolio.position_dates.get(symbol)
                if entry_date and bar.close > avg_price:
                    holding_days = self._count_holding_days(entry_date, portfolio.date)
                    if holding_days >= HOLDING_DAYS:
                        signals.append(prepare.Signal(
                            symbol=symbol, action="sell", weight=1.0,
                            reason=f"Holding {holding_days}d, profitable",
                        ))
                        current_positions.discard(symbol)
                        continue

            # Buy logic
            if not is_held and len(current_positions) < MAX_POSITIONS:
                if rsi <= RSI_OVERSOLD:
                    signals.append(prepare.Signal(
                        symbol=symbol, action="buy", weight=POSITION_SIZE,
                        reason=f"RSI oversold ({rsi:.1f})",
                    ))
                    current_positions.add(symbol)

        return signals
