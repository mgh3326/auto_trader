from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MomentumEventUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_at: dt.datetime
    trading_date: dt.date
    source: str = "naver_stock"
    surface: str
    market: str = "kr"
    trade_type: str | None = None
    market_type: str | None = None
    order_type: str
    rank: int
    symbol: str
    name: str | None = None
    price: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    trade_value: Decimal | None = None
    market_cap: Decimal | None = None
    raw_payload: dict[str, Any] | None = None


class ThemeEventStockUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    rank: int | None = None
    order_type: str | None = None
    price: Decimal | None = None
    change_amount: Decimal | None = None
    change_rate: Decimal | None = None
    volume: int | None = None
    trade_value: Decimal | None = None
    raw_payload: dict[str, Any] | None = None


class ThemeEventUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_at: dt.datetime
    trading_date: dt.date
    source: str = "naver_stock"
    surface: str
    market: str = "kr"
    event_kind: str
    source_event_key: str
    naver_theme_no: str | None = None
    naver_upjong_code: str | None = None
    name: str
    sort_type: str
    rank: int | None = None
    market_type: str | None = None
    change_rate: Decimal | None = None
    trade_value: Decimal | None = None
    market_cap: Decimal | None = None
    stock_count: int | None = None
    leader_symbols: list[dict[str, str | None]] = Field(default_factory=list)
    raw_payload: dict[str, Any] | None = None
    stocks: list[ThemeEventStockUpsert] = Field(default_factory=list)
