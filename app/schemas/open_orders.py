"""Read-only live open-order schemas for /invest current orders (ROB-572)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

OpenOrderMarket = Literal["kr", "us", "crypto"]
OpenOrdersQueryMarket = Literal["all", "kr", "us", "crypto"]
OpenOrderBroker = Literal["kis", "toss", "upbit"]
OpenOrderSide = Literal["buy", "sell", "unknown"]
OpenOrderDataState = Literal["ok", "degraded", "unavailable"]


class OpenOrderRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: OpenOrderBroker
    market: OpenOrderMarket
    symbol: str = Field(min_length=1)
    symbol_name: str | None = None
    side: OpenOrderSide = "unknown"
    order_type: str | None = None
    time_in_force: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    remaining_qty: Decimal | None = None
    filled_qty: Decimal | None = None
    status: str = "pending"
    raw_status: str | None = None
    ordered_at: datetime | None = None
    order_no: str = Field(min_length=1)
    exchange: str | None = None
    currency: str | None = None

    @field_serializer("price", "quantity", "remaining_qty", "filled_qty")
    def _decimal_to_json(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")


class OpenOrderSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: OpenOrderBroker
    market: OpenOrderMarket
    status: OpenOrderDataState
    fetched_at: datetime | None = None
    count: int = Field(ge=0)
    message: str | None = None


class OpenOrdersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: OpenOrdersQueryMarket
    count: int = Field(ge=0)
    data_state: OpenOrderDataState
    as_of: datetime
    items: list[OpenOrderRow]
    sources: list[OpenOrderSourceState]
    warnings: list[str] = Field(default_factory=list)
    empty_reason: str | None = None
