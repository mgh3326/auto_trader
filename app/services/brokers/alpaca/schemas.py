from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class AccountSnapshot(BaseModel):
    id: str
    buying_power: Decimal
    cash: Decimal
    portfolio_value: Decimal
    status: str


class CashBalance(BaseModel):
    cash: Decimal
    buying_power: Decimal


class Position(BaseModel):
    asset_id: str
    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None
    side: str


class Asset(BaseModel):
    id: str
    symbol: str
    name: str | None = None
    status: str
    tradable: bool
    asset_class: str = Field(alias="class", default="us_equity")

    model_config = {"populate_by_name": True}


class OrderRequest(BaseModel):
    symbol: str
    qty: Decimal | None = None
    notional: Decimal | None = None
    side: str
    type: str
    time_in_force: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None


class Order(BaseModel):
    id: str
    client_order_id: str | None = None
    symbol: str
    qty: Decimal | None = None
    notional: Decimal | None = None
    filled_qty: Decimal | None = None
    side: str
    type: str
    time_in_force: str
    status: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    filled_avg_price: Decimal | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None


class Fill(BaseModel):
    id: str
    activity_type: str
    symbol: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    side: str | None = None
    transaction_time: datetime | None = None
    order_id: str | None = None
    cum_qty: Decimal | None = None
    leaves_qty: Decimal | None = None
    order_status: str | None = None
