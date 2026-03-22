"""Buy and hold benchmark strategy."""

import prepare


class BuyAndHold:
    """Buy and hold benchmark strategy.

    Rules:
    - First day only: equally weighted buys across incoming symbols
    - No further actions after initial purchase
    """

    def __init__(self) -> None:
        """Initialize strategy."""
        self._has_bought = False

    def on_bar(
        self,
        date: str,
        bar_data: dict[str, prepare.BarData],
        portfolio: prepare.PortfolioState,
        bar_index: int,
    ) -> list[prepare.Signal]:
        """Generate trading signals.

        On first day only, buy equally weighted positions across all symbols.
        """
        signals: list[prepare.Signal] = []

        if self._has_bought or bar_index > 0:
            return signals

        # Only buy on first day
        symbols = list(bar_data.keys())
        if not symbols:
            return signals

        # Equal weight for each symbol
        weight = 1.0 / len(symbols)

        for symbol in symbols:
            signals.append(prepare.Signal(
                symbol=symbol,
                action="buy",
                target_weight=weight,
            ))

        self._has_bought = True
        return signals
