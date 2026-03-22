"""Backtest strategy implementation."""

import numpy as np
import prepare

# Strategy Constants
RSI_PERIOD_FAST = 7
RSI_PERIOD_SLOW = 14
RSI_OVERSOLD = 30
RSI_EXIT = 50  # Exit when RSI recovers to neutral
MAX_POSITIONS = 5
POSITION_SIZE = 0.15
HOLDING_DAYS = 14
STOP_LOSS_PCT = 0.08  # Tighter stop-loss


def _calc_rsi(closes: np.ndarray, period: int) -> float | None:
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
    """Dual RSI mean-reversion: buy oversold, exit at RSI neutral."""

    def __init__(self) -> None:
        pass

    def _get_rsi(self, bar: prepare.BarData, period: int) -> float | None:
        if len(bar.history) < period + 1:
            return None
        closes = bar.history["close"].values
        return _calc_rsi(closes, period)

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
            rsi_fast = self._get_rsi(bar, RSI_PERIOD_FAST)
            rsi_slow = self._get_rsi(bar, RSI_PERIOD_SLOW)
            if rsi_slow is None:
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

                # Exit when RSI recovers to neutral (mean-reversion exit)
                if rsi_slow >= RSI_EXIT and bar.close > avg_price:
                    signals.append(prepare.Signal(
                        symbol=symbol, action="sell", weight=1.0,
                        reason=f"RSI recovered to {rsi_slow:.0f}, profitable",
                    ))
                    current_positions.discard(symbol)
                    continue

                # Sell on holding period exceeded (regardless of profit)
                entry_date = portfolio.position_dates.get(symbol)
                if entry_date:
                    holding_days = self._count_holding_days(entry_date, portfolio.date)
                    if holding_days >= HOLDING_DAYS:
                        signals.append(prepare.Signal(
                            symbol=symbol, action="sell", weight=1.0,
                            reason=f"Max holding {holding_days}d",
                        ))
                        current_positions.discard(symbol)
                        continue

            # Buy logic: both RSIs must be oversold
            if not is_held and len(current_positions) < MAX_POSITIONS:
                both_oversold = rsi_slow <= RSI_OVERSOLD and (rsi_fast is not None and rsi_fast <= RSI_OVERSOLD)
                if both_oversold:
                    signals.append(prepare.Signal(
                        symbol=symbol, action="buy", weight=POSITION_SIZE,
                        reason=f"Dual RSI oversold (f={rsi_fast:.0f}, s={rsi_slow:.0f})",
                    ))
                    current_positions.add(symbol)

        return signals
