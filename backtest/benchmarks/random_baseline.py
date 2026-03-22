"""Random baseline benchmark strategy."""

import random

import prepare


class RandomBaseline:
    """Random baseline benchmark strategy.

    Rules:
    - Fixed seed for reproducibility
    - Low-frequency signals (only acts on some days)
    - No shorting (only buys or sells existing positions)
    - Avoids generating invalid sell signals for unheld symbols
    """

    def __init__(self, seed: int = 42, action_probability: float = 0.1) -> None:
        """Initialize strategy.

        Args:
            seed: Random seed for reproducibility
            action_probability: Probability of taking action on any given day
        """
        self._rng = random.Random(seed)
        self._action_probability = action_probability

    def on_bar(
        self,
        date: str,
        bar_data: dict[str, prepare.BarData],
        portfolio: prepare.PortfolioState,
        bar_index: int,
    ) -> list[prepare.Signal]:
        """Generate random trading signals."""
        signals: list[prepare.Signal] = []

        # Low frequency: only act sometimes
        if self._rng.random() > self._action_probability:
            return signals

        # Get available symbols
        available_symbols = list(bar_data.keys())
        if not available_symbols:
            return signals

        # Randomly choose action type
        current_positions = set(portfolio.positions.keys())

        # Can buy symbols we don't hold
        can_buy = [s for s in available_symbols if s not in current_positions]
        # Can sell symbols we hold
        can_sell = [s for s in current_positions if s in available_symbols]

        if not can_buy and not can_sell:
            return signals

        # Randomly decide whether to buy or sell
        if can_buy and (not can_sell or self._rng.random() < 0.5):
            # Buy a random symbol
            symbol = self._rng.choice(can_buy)
            signals.append(prepare.Signal(
                symbol=symbol,
                action="buy",
                target_weight=0.15,  # Fixed position size
            ))
        elif can_sell:
            # Sell a random held symbol
            symbol = self._rng.choice(can_sell)
            signals.append(prepare.Signal(
                symbol=symbol,
                action="sell",
                target_weight=0.0,  # Full sell
            ))

        return signals
