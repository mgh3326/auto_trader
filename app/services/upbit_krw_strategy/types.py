"""Plain input/output records. No behaviour beyond boundary validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


def require_decimal(value: object, name: str) -> Decimal:
    """Exact finite Decimal only; a float has already lost the exact value."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    return value


@dataclass(frozen=True)
class DailyBar:
    """One daily candle. ``day`` is the UTC open date (00:00 UTC = 09:00 KST)."""

    day: date
    close: Decimal
    quote_volume: Decimal | None
    is_closed: bool

    def __post_init__(self) -> None:
        require_decimal(self.close, "close")
        if self.quote_volume is not None:
            require_decimal(self.quote_volume, "quote_volume")


@dataclass(frozen=True)
class MarketInput:
    """What the universe rules read for one KRW market."""

    market: str
    is_active: bool
    market_warning: str
    bars: tuple[DailyBar, ...]
