"""hk 5441 — the two account reads the Upbit path makes before it reserves.

Both are reads through the functions the existing guards already use, so the
gateway's early look and the guard's own later check see the same source:

* the account's average buy price for a symbol, which the loss-sell guard
  compares a sell against (I10, B9);
* the stop-loss cooldown of a symbol, which blocks a buy (I10).

Neither result is authority to send. The order path below runs both guards
itself, unchanged.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


async def upbit_avg_buy_price(symbol: str) -> Decimal | None:
    """The account's average buy price, or ``None`` when it is not provable."""
    from app.mcp_server.tooling.order_validation import _get_holdings_for_order

    holdings = await _get_holdings_for_order(symbol, "crypto")
    if not holdings:
        return None
    raw = holdings.get("avg_price")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


async def upbit_cooldown_active(symbol: str) -> bool:
    from app.mcp_server.tooling.order_execution import (
        _get_crypto_trade_cooldown_service,
    )

    return bool(await _get_crypto_trade_cooldown_service().is_in_cooldown(symbol))
