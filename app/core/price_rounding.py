"""Market price rounding and lenient float coercion.

Shared by MCP tooling (``app.mcp_server.tooling.shared`` re-exports these) and
by pure services that must not import MCP tooling.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.upbit_tick import upbit_krw_tick

#: Markets whose prices snap to the Upbit KRW order tick. Every other market
#: (equity_kr, equity_us, None) keeps the historical two-decimal rounding.
_TICK_PRICED_MARKETS = frozenset({"crypto"})


def round_price_for_market(value: float, market_type: str | None) -> float:
    """Round a price to the precision its instrument actually trades at.

    Crypto (Upbit KRW) prices snap to the Upbit order tick
    (``app.core.upbit_tick``), so sub-0.01 coins such as KRW-XEC keep distinct
    levels instead of collapsing to 0.0/0.01. KR and US keep ``round(value, 2)``,
    which is already at or finer than their tick. ``None`` means the equity
    path. Percentages and indicator values (RSI, ADX, distance_pct) must keep
    their own 2-decimal rounding and must not use this helper.
    """
    if market_type not in _TICK_PRICED_MARKETS:
        return round(value, 2)
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        return price
    exact = Decimal(repr(price))
    tick = upbit_krw_tick(exact)
    snapped = (exact / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return float(snapped)


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def to_optional_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None
