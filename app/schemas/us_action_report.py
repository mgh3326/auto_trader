from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PriceStateLiteral = Literal["live", "stale", "missing"]
SourceLiteral = Literal["kis_live"]
OrderSideLiteral = Literal["buy", "sell", "unknown"]


class USOpenOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    side: OrderSideLiteral = "unknown"
    quantity: float | None = None
    remaining_qty: float | None = Field(default=None, alias="remainingQty")
    pending_qty: float = Field(default=0.0, alias="pendingQty")
    order_id: str | None = Field(default=None, alias="orderId")


class USHolding(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    display_name: str = Field(alias="displayName")
    quantity: float
    average_cost_usd: float | None = Field(default=None, alias="averageCostUsd")
    cost_basis_usd: float | None = Field(default=None, alias="costBasisUsd")
    last_price_usd: float | None = Field(default=None, alias="lastPriceUsd")
    value_usd: float | None = Field(default=None, alias="valueUsd")
    pnl_usd: float | None = Field(default=None, alias="pnlUsd")
    pnl_rate: float | None = Field(default=None, alias="pnlRate")
    price_state: PriceStateLiteral = Field(default="live", alias="priceState")
    source_of_truth: bool = Field(default=True, alias="sourceOfTruth")
    is_tradeable: bool = Field(default=True, alias="isTradeable")
    manual_only: bool = Field(default=False, alias="manualOnly")
    sellable_qty: float = Field(default=0.0, alias="sellableQty")
    pending_qty: float = Field(default=0.0, alias="pendingQty")


class KISUSAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    captured_at: datetime = Field(alias="capturedAt")
    source: SourceLiteral = "kis_live"
    source_of_truth: bool = Field(default=True, alias="sourceOfTruth")
    is_tradeable: bool = Field(default=True, alias="isTradeable")
    manual_only: bool = Field(default=False, alias="manualOnly")
    usd_cash: float | None = Field(default=None, alias="usdCash")
    usd_buying_power: float | None = Field(default=None, alias="usdBuyingPower")
    holdings: list[USHolding] = Field(default_factory=list)
    open_orders: list[USOpenOrder] = Field(default_factory=list, alias="openOrders")
    warnings: list[str] = Field(default_factory=list)
