from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CashBalance:
    market: str
    currency: str
    balance: float
    orderable: float
    source: str


@dataclass(slots=True)
class Position:
    symbol: str
    market: str
    source: str
    quantity: float
    avg_price: float | None
    current_price: float | None
    evaluation_amount: float | None
    profit_loss: float | None
    profit_rate: float | None
    name: str | None = None


@dataclass(slots=True)
class MarginSnapshot:
    market: str
    source: str
    details: dict[str, Any]


__all__ = ["CashBalance", "Position", "MarginSnapshot"]
