from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ParseResult[T]:
    rows: tuple[T, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NaverStockRow:
    symbol: str
    name: str | None
    rank: int | None
    price: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    trade_value: Decimal | None = None
    market_cap: Decimal | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NaverThemeRow:
    event_kind: str
    source_key: str
    name: str
    rank: int | None
    naver_theme_no: str | None = None
    naver_upjong_code: str | None = None
    change_rate: Decimal | None = None
    trade_value: Decimal | None = None
    market_cap: Decimal | None = None
    stock_count: int | None = None
    leader_symbols: tuple[dict[str, str | None], ...] = ()
    raw_payload: dict[str, Any] = field(default_factory=dict)
