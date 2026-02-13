"""KRX (Korean Exchange) tick size adjustment for Korean equities.

This module provides tick size adjustment logic following KRX's standard
tick size table, which is required for placing limit orders on Korean stocks.

Based on KRX market rules (2023+):
- Buy orders: Round DOWN (floor) to nearest tick
- Sell orders: Round UP (ceil) to nearest tick

References:
- KRX 유가증권시장 매매거래제도 일반: https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100.jsp
- KRX 코스닥시장 매매거래제도 일반: https://regulation.krx.co.kr/contents/RGL/03/03020100/RGL03020100.jsp

KRX Tick Size Table (KRW, 2023+):
| Price Range       | Tick Size |
|-------------------|-----------|
| ~2,000            | 1         |
| 2,000-5,000       | 5         |
| 5,000-20,000      | 10        |
| 20,000-50,000     | 50        |
| 50,000-200,000    | 100       |
| 200,000-500,000   | 500       |
| 500,000~          | 1,000     |
"""

import math


def get_tick_size_kr(price: float) -> int:
    """Return the tick size for a given price based on KRX rules (2023+).

    Args:
        price: Stock price in KRW

    Returns:
        Tick size in KRW
    """
    if price < 2000:
        return 1
    elif price < 5000:
        return 5
    elif price < 20000:
        return 10
    elif price < 50000:
        return 50
    elif price < 200000:
        return 100
    elif price < 500000:
        return 500
    else:
        return 1000


def _get_tick_size(price: float) -> int:
    """Return the tick size for a given price based on KRX rules.

    DEPRECATED: Use get_tick_size_kr() instead.

    Args:
        price: Stock price in KRW

    Returns:
        Tick size in KRW
    """
    return get_tick_size_kr(price)


def adjust_tick_size_kr(price: float, side: str = "buy") -> int:
    """Adjust price to KRX tick size rules.

    For Korean stocks (equity_kr), prices must align with tick sizes:
    - Buy orders: Round DOWN (floor) - lower price for better execution
    - Sell orders: Round UP (ceil) - higher price for better execution

    Args:
        price: Price to adjust in KRW
        side: Order side ("buy" or "sell")

    Returns:
        Adjusted price in KRW (integer)

    Examples:
        >>> adjust_tick_size_kr(327272, "buy")
        327000
        >>> adjust_tick_size_kr(327272, "sell")
        327500
        >>> adjust_tick_size_kr(1098000, "buy")
        1098000
        >>> adjust_tick_size_kr(15723, "buy")
        15720
    """
    if price < 0:
        raise ValueError(f"Price must be non-negative, got {price}")

    tick_size = _get_tick_size(price)

    # Normalize to tick size boundaries
    if side == "buy":
        # Round DOWN (floor) - better for buy orders (lower price)
        adjusted = math.floor(price / tick_size) * tick_size
    elif side == "sell":
        # Round UP (ceil) - better for sell orders (higher price)
        adjusted = math.ceil(price / tick_size) * tick_size
    else:
        raise ValueError(f"side must be 'buy' or 'sell', got '{side}'")

    # Ensure minimum price of 1 KRW
    adjusted = max(1, int(adjusted))

    return adjusted
