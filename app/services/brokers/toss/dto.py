from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def parse_decimal_string(value: object) -> Decimal:
    if isinstance(value, float):
        raise TypeError("Toss decimal values must be strings, not float")
    if value is None:
        raise TypeError("Toss decimal value is required")
    return Decimal(str(value))


def parse_optional_decimal_string(value: object) -> Decimal | None:
    if value is None:
        return None
    return parse_decimal_string(value)


def _decimal_map(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: parse_optional_decimal_string(value)
        if value is None or isinstance(value, str | int | float)
        else value
        for key, value in raw.items()
    }


@dataclass(frozen=True)
class TossAccount:
    account_no: str
    account_seq: int
    account_type: str


@dataclass(frozen=True)
class TossPrice:
    symbol: str
    timestamp: str | None
    last_price: Decimal
    currency: str


@dataclass(frozen=True)
class TossStockInfo:
    symbol: str
    name: str
    english_name: str
    isin_code: str
    market: str
    security_type: str
    is_common_share: bool
    status: str
    currency: str
    list_date: str | None
    delist_date: str | None
    shares_outstanding: Decimal
    leverage_factor: Decimal | None
    korean_market_detail: dict[str, Any] | None


@dataclass(frozen=True)
class TossHoldingItem:
    symbol: str
    name: str
    market_country: str
    currency: str
    quantity: Decimal
    last_price: Decimal
    average_purchase_price: Decimal
    market_value: dict[str, Any]
    profit_loss: dict[str, Any]
    daily_profit_loss: dict[str, Any]
    cost: dict[str, Any]


@dataclass(frozen=True)
class TossHoldings:
    items: list[TossHoldingItem] = field(default_factory=list)
    raw_overview: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TossOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    price: Decimal | None
    quantity: Decimal
    order_amount: Decimal | None
    currency: str
    ordered_at: str
    canceled_at: str | None
    execution: dict[str, Any]


@dataclass(frozen=True)
class TossOrdersPage:
    orders: list[TossOrder]
    next_cursor: str | None
    has_next: bool


def parse_accounts(raw: list[dict[str, Any]]) -> list[TossAccount]:
    return [
        TossAccount(
            account_no=str(row["accountNo"]),
            account_seq=int(row["accountSeq"]),
            account_type=str(row["accountType"]),
        )
        for row in raw
    ]


def parse_prices(raw: list[dict[str, Any]]) -> list[TossPrice]:
    return [
        TossPrice(
            symbol=str(row["symbol"]),
            timestamp=row.get("timestamp"),
            last_price=parse_decimal_string(row["lastPrice"]),
            currency=str(row["currency"]),
        )
        for row in raw
    ]


def parse_stocks(raw: list[dict[str, Any]]) -> list[TossStockInfo]:
    return [
        TossStockInfo(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            english_name=str(row["englishName"]),
            isin_code=str(row["isinCode"]),
            market=str(row["market"]),
            security_type=str(row["securityType"]),
            is_common_share=bool(row["isCommonShare"]),
            status=str(row["status"]),
            currency=str(row["currency"]),
            list_date=row.get("listDate"),
            delist_date=row.get("delistDate"),
            shares_outstanding=parse_decimal_string(row["sharesOutstanding"]),
            leverage_factor=parse_optional_decimal_string(row.get("leverageFactor")),
            korean_market_detail=row.get("koreanMarketDetail"),
        )
        for row in raw
    ]


def parse_holdings(raw: dict[str, Any]) -> TossHoldings:
    items = []
    for row in raw.get("items", []):
        items.append(
            TossHoldingItem(
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                market_country=str(row["marketCountry"]),
                currency=str(row["currency"]),
                quantity=parse_decimal_string(row["quantity"]),
                last_price=parse_decimal_string(row["lastPrice"]),
                average_purchase_price=parse_decimal_string(
                    row["averagePurchasePrice"]
                ),
                market_value=_decimal_map(dict(row["marketValue"])),
                profit_loss=_decimal_map(dict(row["profitLoss"])),
                daily_profit_loss=_decimal_map(dict(row["dailyProfitLoss"])),
                cost=_decimal_map(dict(row["cost"])),
            )
        )
    overview = {key: value for key, value in raw.items() if key != "items"}
    return TossHoldings(items=items, raw_overview=overview)


def _parse_execution(raw: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(raw)
    for key in (
        "filledQuantity",
        "averageFilledPrice",
        "filledAmount",
        "commission",
        "tax",
    ):
        if key in parsed:
            parsed[key] = parse_optional_decimal_string(parsed[key])
    return parsed


def parse_orders(raw: dict[str, Any]) -> TossOrdersPage:
    orders = []
    for row in raw.get("orders", []):
        orders.append(
            TossOrder(
                order_id=str(row["orderId"]),
                symbol=str(row["symbol"]),
                side=str(row["side"]),
                order_type=str(row["orderType"]),
                time_in_force=str(row["timeInForce"]),
                status=str(row["status"]),
                price=parse_optional_decimal_string(row.get("price")),
                quantity=parse_decimal_string(row["quantity"]),
                order_amount=parse_optional_decimal_string(row.get("orderAmount")),
                currency=str(row["currency"]),
                ordered_at=str(row["orderedAt"]),
                canceled_at=row.get("canceledAt"),
                execution=_parse_execution(dict(row.get("execution") or {})),
            )
        )
    return TossOrdersPage(
        orders=orders,
        next_cursor=raw.get("nextCursor"),
        has_next=bool(raw.get("hasNext", False)),
    )


@dataclass(frozen=True)
class TossBuyingPower:
    currency: str
    cash_buying_power: Decimal


@dataclass(frozen=True)
class TossSellableQuantity:
    sellable_quantity: Decimal


@dataclass(frozen=True)
class TossCommission:
    market_country: str
    commission_rate: Decimal
    start_date: str | None
    end_date: str | None


def parse_buying_power(raw: dict[str, Any]) -> TossBuyingPower:
    return TossBuyingPower(
        currency=str(raw["currency"]),
        cash_buying_power=parse_decimal_string(raw["cashBuyingPower"]),
    )


def parse_sellable_quantity(raw: dict[str, Any]) -> TossSellableQuantity:
    return TossSellableQuantity(
        sellable_quantity=parse_decimal_string(raw["sellableQuantity"])
    )


def parse_commissions(raw: list[dict[str, Any]]) -> list[TossCommission]:
    return [
        TossCommission(
            market_country=str(row["marketCountry"]),
            commission_rate=parse_decimal_string(row["commissionRate"]),
            start_date=row.get("startDate"),
            end_date=row.get("endDate"),
        )
        for row in raw
    ]


def parse_order(raw: dict[str, Any]) -> TossOrder:
    return parse_orders({"orders": [raw], "nextCursor": None, "hasNext": False}).orders[
        0
    ]
