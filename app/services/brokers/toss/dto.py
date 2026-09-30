from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.brokers.toss.errors import TossResponseContractError


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
    client_order_id: str | None = None


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
                client_order_id=(
                    str(row["clientOrderId"])
                    if row.get("clientOrderId") is not None
                    else None
                ),
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


@dataclass(frozen=True)
class TossOrderPlacementResult:
    order_id: str
    client_order_id: str | None


@dataclass(frozen=True)
class TossOrderOperationResult:
    order_id: str


def parse_order_placement_result(raw: dict[str, Any]) -> TossOrderPlacementResult:
    return TossOrderPlacementResult(
        order_id=str(raw["orderId"]),
        client_order_id=(
            str(raw["clientOrderId"]) if raw.get("clientOrderId") is not None else None
        ),
    )


def parse_order_operation_result(raw: dict[str, Any]) -> TossOrderOperationResult:
    return TossOrderOperationResult(order_id=str(raw["orderId"]))


@dataclass(frozen=True)
class TossCandle:
    timestamp: str
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str


@dataclass(frozen=True)
class TossCandlesPage:
    candles: list[TossCandle]
    next_before: str | None


def parse_candles(raw: dict[str, Any]) -> TossCandlesPage:
    if "candles" not in raw or not isinstance(raw["candles"], list):
        raise ValueError("Malformed Toss candles response: expected candles list")
    candles = [
        TossCandle(
            timestamp=str(row["timestamp"]),
            open_price=parse_decimal_string(row["openPrice"]),
            high_price=parse_decimal_string(row["highPrice"]),
            low_price=parse_decimal_string(row["lowPrice"]),
            close_price=parse_decimal_string(row["closePrice"]),
            volume=parse_decimal_string(row["volume"]),
            currency=str(row["currency"]),
        )
        for row in raw["candles"]
    ]
    next_before = raw.get("nextBefore")
    return TossCandlesPage(
        candles=candles,
        next_before=str(next_before) if next_before is not None else None,
    )


@dataclass(frozen=True)
class TossWarningInfo:
    warning_type: str
    exchange: str | None
    start_date: str | None
    end_date: str | None


def parse_warnings(raw: list[dict[str, Any]]) -> list[TossWarningInfo]:
    return [
        TossWarningInfo(
            warning_type=str(row["warningType"]),
            exchange=row.get("exchange"),
            start_date=row.get("startDate"),
            end_date=row.get("endDate"),
        )
        for row in raw
    ]


# ---------------------------------------------------------------------------
# #1064 read-only market-data surfaces.
#
# Field names below quote the official Toss Open API schemas verbatim
# (openapi.tossinvest.com/openapi-docs/latest/openapi.json, v1.2.19):
#   GET /api/v1/stocks/{symbol}/investor-trading   -> StockInvestorTradingResponse
#   GET /api/v1/market-indicators/prices          -> [MarketIndicatorPriceResponse]
#   GET /api/v1/market-indicators/{s}/investor-trading -> InvestorTradingResponse
#   GET /api/v1/rankings                          -> RankingResponse
#   GET /api/v1/stocks/all                        -> [ListedStock]
# Unknown extra fields are ignored; a missing required field raises
# TossResponseContractError.
# ---------------------------------------------------------------------------

_CONTRACT_FIELDS = (KeyError, TypeError, ValueError, ArithmeticError)


@dataclass(frozen=True)
class TossInvestorTradingVolume:
    """InvestorTradingVolume: buyVolume/sellVolume/netBuyVolume (주, 정수)."""

    buy_volume: Decimal
    sell_volume: Decimal
    net_buy_volume: Decimal


@dataclass(frozen=True)
class TossStockInstitutionBreakdown:
    """StockInstitutionTradingBreakdown: 7 institution subclasses, all required."""

    financial_investment: TossInvestorTradingVolume
    insurance: TossInvestorTradingVolume
    trust: TossInvestorTradingVolume
    private_equity_fund: TossInvestorTradingVolume
    bank: TossInvestorTradingVolume
    other_financial_institution: TossInvestorTradingVolume
    pension_fund: TossInvestorTradingVolume


@dataclass(frozen=True)
class TossStockInstitutionTrading:
    """StockInstitutionTradingVolume: totals plus a nullable 7-way breakdown."""

    buy_volume: Decimal
    sell_volume: Decimal
    net_buy_volume: Decimal
    breakdown: TossStockInstitutionBreakdown | None


@dataclass(frozen=True)
class TossForeignerHolding:
    """ForeignerHolding: holdingQuantity/limitQuantity/holdingRate."""

    holding_quantity: Decimal
    limit_quantity: Decimal
    holding_rate: Decimal


@dataclass(frozen=True)
class TossCfdBalance:
    """CfdBalance: buyBalanceQuantity/buyBalanceRate/sellBalanceQuantity/sellBalanceRate."""

    buy_balance_quantity: Decimal
    buy_balance_rate: Decimal
    sell_balance_quantity: Decimal
    sell_balance_rate: Decimal


@dataclass(frozen=True)
class TossStockInvestorTradingRecord:
    """StockInvestorTradingRecord (required: date/updatedAt/foreigner/institution).

    ``individual``, ``other_corporation``, ``foreigner_holding`` and ``cfd``
    are documented nullable — intraday records are provisional and carry nulls
    until confirmed values are applied.
    """

    date: str
    updated_at: str
    foreigner: TossInvestorTradingVolume
    institution: TossStockInstitutionTrading
    individual: TossInvestorTradingVolume | None = None
    other_corporation: TossInvestorTradingVolume | None = None
    foreigner_holding: TossForeignerHolding | None = None
    cfd: TossCfdBalance | None = None


@dataclass(frozen=True)
class TossStockInvestorTradingPage:
    """StockInvestorTradingResponse: ``records`` + cursor ``nextUntil``."""

    records: list[TossStockInvestorTradingRecord]
    next_until: str | None


def _parse_investor_volume(raw: dict[str, Any]) -> TossInvestorTradingVolume:
    return TossInvestorTradingVolume(
        buy_volume=parse_decimal_string(raw["buyVolume"]),
        sell_volume=parse_decimal_string(raw["sellVolume"]),
        net_buy_volume=parse_decimal_string(raw["netBuyVolume"]),
    )


def _parse_optional_investor_volume(
    raw: dict[str, Any] | None,
) -> TossInvestorTradingVolume | None:
    if raw is None:
        return None
    return _parse_investor_volume(raw)


def _parse_stock_institution_breakdown(
    raw: dict[str, Any],
) -> TossStockInstitutionBreakdown:
    return TossStockInstitutionBreakdown(
        financial_investment=_parse_investor_volume(raw["financialInvestment"]),
        insurance=_parse_investor_volume(raw["insurance"]),
        trust=_parse_investor_volume(raw["trust"]),
        private_equity_fund=_parse_investor_volume(raw["privateEquityFund"]),
        bank=_parse_investor_volume(raw["bank"]),
        other_financial_institution=_parse_investor_volume(
            raw["otherFinancialInstitution"]
        ),
        pension_fund=_parse_investor_volume(raw["pensionFund"]),
    )


def _parse_stock_institution_trading(
    raw: dict[str, Any],
) -> TossStockInstitutionTrading:
    breakdown_raw = raw.get("breakdown")
    return TossStockInstitutionTrading(
        buy_volume=parse_decimal_string(raw["buyVolume"]),
        sell_volume=parse_decimal_string(raw["sellVolume"]),
        net_buy_volume=parse_decimal_string(raw["netBuyVolume"]),
        breakdown=(
            None
            if breakdown_raw is None
            else _parse_stock_institution_breakdown(breakdown_raw)
        ),
    )


def _parse_foreigner_holding(raw: dict[str, Any] | None) -> TossForeignerHolding | None:
    if raw is None:
        return None
    return TossForeignerHolding(
        holding_quantity=parse_decimal_string(raw["holdingQuantity"]),
        limit_quantity=parse_decimal_string(raw["limitQuantity"]),
        holding_rate=parse_decimal_string(raw["holdingRate"]),
    )


def _parse_cfd_balance(raw: dict[str, Any] | None) -> TossCfdBalance | None:
    if raw is None:
        return None
    return TossCfdBalance(
        buy_balance_quantity=parse_decimal_string(raw["buyBalanceQuantity"]),
        buy_balance_rate=parse_decimal_string(raw["buyBalanceRate"]),
        sell_balance_quantity=parse_decimal_string(raw["sellBalanceQuantity"]),
        sell_balance_rate=parse_decimal_string(raw["sellBalanceRate"]),
    )


def _parse_stock_investor_record(
    raw: dict[str, Any],
) -> TossStockInvestorTradingRecord:
    return TossStockInvestorTradingRecord(
        date=str(raw["date"]),
        updated_at=str(raw["updatedAt"]),
        foreigner=_parse_investor_volume(raw["foreigner"]),
        institution=_parse_stock_institution_trading(raw["institution"]),
        individual=_parse_optional_investor_volume(raw.get("individual")),
        other_corporation=_parse_optional_investor_volume(raw.get("otherCorporation")),
        foreigner_holding=_parse_foreigner_holding(raw.get("foreignerHolding")),
        cfd=_parse_cfd_balance(raw.get("cfd")),
    )


def parse_stock_investor_trading_page(
    raw: dict[str, Any],
) -> TossStockInvestorTradingPage:
    try:
        records_raw = raw["records"]
        if not isinstance(records_raw, list):
            raise TossResponseContractError(
                "stocks/{symbol}/investor-trading: 'records' must be a list"
            )
        records = [_parse_stock_investor_record(row) for row in records_raw]
        next_until = raw.get("nextUntil")
        return TossStockInvestorTradingPage(
            records=records,
            next_until=str(next_until) if next_until is not None else None,
        )
    except TossResponseContractError:
        raise
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "stocks/{symbol}/investor-trading payload violates the documented schema"
        ) from exc


@dataclass(frozen=True)
class TossMarketIndicatorPrice:
    """MarketIndicatorPriceResponse: required symbol/lastPrice; timestamp nullable."""

    symbol: str
    timestamp: str | None
    last_price: Decimal


def parse_market_indicator_prices(
    raw: list[dict[str, Any]],
) -> list[TossMarketIndicatorPrice]:
    try:
        return [
            TossMarketIndicatorPrice(
                symbol=str(row["symbol"]),
                timestamp=row.get("timestamp"),
                last_price=parse_decimal_string(row["lastPrice"]),
            )
            for row in raw
        ]
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "market-indicators/prices payload violates the documented schema"
        ) from exc


@dataclass(frozen=True)
class TossInvestorTradingAmount:
    """InvestorTradingAmount: buyAmount/sellAmount (KRW, 정수)."""

    buy_amount: Decimal
    sell_amount: Decimal


@dataclass(frozen=True)
class TossMarketInstitutionBreakdown:
    """InstitutionTradingBreakdown: 7 subclasses of InvestorTradingAmount."""

    financial_investment: TossInvestorTradingAmount
    insurance: TossInvestorTradingAmount
    trust: TossInvestorTradingAmount
    private_equity_fund: TossInvestorTradingAmount
    bank: TossInvestorTradingAmount
    other_financial_institution: TossInvestorTradingAmount
    pension_fund: TossInvestorTradingAmount


@dataclass(frozen=True)
class TossMarketInstitutionTrading:
    """InstitutionTradingAmount: totals plus the required 7-way breakdown."""

    buy_amount: Decimal
    sell_amount: Decimal
    breakdown: TossMarketInstitutionBreakdown


@dataclass(frozen=True)
class TossMarketInvestorTradingRecord:
    """InvestorTradingRecord: all four investor classes are required."""

    date: str
    updated_at: str
    individual: TossInvestorTradingAmount
    foreigner: TossInvestorTradingAmount
    institution: TossMarketInstitutionTrading
    other_corporation: TossInvestorTradingAmount


@dataclass(frozen=True)
class TossMarketInvestorTradingPage:
    """InvestorTradingResponse: ``records`` + cursor ``nextUntil``."""

    records: list[TossMarketInvestorTradingRecord]
    next_until: str | None


def _parse_trading_amount(raw: dict[str, Any]) -> TossInvestorTradingAmount:
    return TossInvestorTradingAmount(
        buy_amount=parse_decimal_string(raw["buyAmount"]),
        sell_amount=parse_decimal_string(raw["sellAmount"]),
    )


def _parse_market_institution_trading(
    raw: dict[str, Any],
) -> TossMarketInstitutionTrading:
    breakdown_raw = raw["breakdown"]
    return TossMarketInstitutionTrading(
        buy_amount=parse_decimal_string(raw["buyAmount"]),
        sell_amount=parse_decimal_string(raw["sellAmount"]),
        breakdown=TossMarketInstitutionBreakdown(
            financial_investment=_parse_trading_amount(
                breakdown_raw["financialInvestment"]
            ),
            insurance=_parse_trading_amount(breakdown_raw["insurance"]),
            trust=_parse_trading_amount(breakdown_raw["trust"]),
            private_equity_fund=_parse_trading_amount(
                breakdown_raw["privateEquityFund"]
            ),
            bank=_parse_trading_amount(breakdown_raw["bank"]),
            other_financial_institution=_parse_trading_amount(
                breakdown_raw["otherFinancialInstitution"]
            ),
            pension_fund=_parse_trading_amount(breakdown_raw["pensionFund"]),
        ),
    )


def _parse_market_investor_record(
    raw: dict[str, Any],
) -> TossMarketInvestorTradingRecord:
    return TossMarketInvestorTradingRecord(
        date=str(raw["date"]),
        updated_at=str(raw["updatedAt"]),
        individual=_parse_trading_amount(raw["individual"]),
        foreigner=_parse_trading_amount(raw["foreigner"]),
        institution=_parse_market_institution_trading(raw["institution"]),
        other_corporation=_parse_trading_amount(raw["otherCorporation"]),
    )


def parse_market_investor_trading_page(
    raw: dict[str, Any],
) -> TossMarketInvestorTradingPage:
    try:
        records_raw = raw["records"]
        if not isinstance(records_raw, list):
            raise TossResponseContractError(
                "market-indicators/{symbol}/investor-trading: 'records' must be a list"
            )
        records = [_parse_market_investor_record(row) for row in records_raw]
        next_until = raw.get("nextUntil")
        return TossMarketInvestorTradingPage(
            records=records,
            next_until=str(next_until) if next_until is not None else None,
        )
    except TossResponseContractError:
        raise
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "market-indicators/{symbol}/investor-trading payload violates the "
            "documented schema"
        ) from exc


@dataclass(frozen=True)
class TossRankingPrice:
    """RankingPrice: required lastPrice/basePrice; changeRate nullable."""

    last_price: Decimal
    base_price: Decimal
    change_rate: Decimal | None


@dataclass(frozen=True)
class TossRankingItem:
    """RankingItem (required: rank/symbol/currency/price/tradingVolume/tradingAmount)."""

    rank: int
    symbol: str
    currency: str
    price: TossRankingPrice
    trading_volume: Decimal
    trading_amount: Decimal


@dataclass(frozen=True)
class TossRankings:
    """RankingResponse: ``rankings`` required; ``rankedAt`` null when empty."""

    rankings: list[TossRankingItem]
    ranked_at: str | None


def parse_rankings(raw: dict[str, Any]) -> TossRankings:
    try:
        rankings_raw = raw["rankings"]
        if not isinstance(rankings_raw, list):
            raise TossResponseContractError("rankings: 'rankings' must be a list")
        items = []
        for row in rankings_raw:
            price_raw = row["price"]
            items.append(
                TossRankingItem(
                    rank=int(row["rank"]),
                    symbol=str(row["symbol"]),
                    currency=str(row["currency"]),
                    price=TossRankingPrice(
                        last_price=parse_decimal_string(price_raw["lastPrice"]),
                        base_price=parse_decimal_string(price_raw["basePrice"]),
                        change_rate=parse_optional_decimal_string(
                            price_raw.get("changeRate")
                        ),
                    ),
                    trading_volume=parse_decimal_string(row["tradingVolume"]),
                    trading_amount=parse_decimal_string(row["tradingAmount"]),
                )
            )
        ranked_at = raw.get("rankedAt")
        return TossRankings(
            rankings=items,
            ranked_at=str(ranked_at) if ranked_at is not None else None,
        )
    except TossResponseContractError:
        raise
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "rankings payload violates the documented schema"
        ) from exc


@dataclass(frozen=True)
class TossListedStock:
    """ListedStock (required: symbol/name/securityType/isCommonShare/isinCode).

    The schema carries no ``status`` field — DELISTED rows surface only via the
    ``status=DELISTED`` query filter.
    """

    symbol: str
    name: str
    security_type: str
    is_common_share: bool
    isin_code: str


def parse_listed_stocks(raw: list[dict[str, Any]]) -> list[TossListedStock]:
    try:
        return [
            TossListedStock(
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                security_type=str(row["securityType"]),
                is_common_share=bool(row["isCommonShare"]),
                isin_code=str(row["isinCode"]),
            )
            for row in raw
        ]
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "stocks/all payload violates the documented schema"
        ) from exc
