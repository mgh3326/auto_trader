from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class OrderResult:
    order_id: str | None
    status: str
    market: str
    symbol: str
    side: str | None
    order_type: str | None
    source: str
    raw: dict[str, Any]


__all__ = ["OrderResult"]
