from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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

_CONTRACT_FIELDS = (KeyError, TypeError, ValueError, ArithmeticError, AttributeError)


def _req_map(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TossResponseContractError(f"'{field}' must be a JSON object")
    return value


def _opt_map(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _req_map(value, field)


def _req_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TossResponseContractError(f"'{field}' must be a string")
    return value


def _opt_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _req_str(value, field)


def _req_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TossResponseContractError(f"'{field}' must be an integer")
    return value


def _req_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TossResponseContractError(f"'{field}' must be a boolean")
    return value


def _req_decimal(value: Any, field: str) -> Decimal:
    # Official schemas type these as decimal strings; a bare JSON number or a
    # non-finite literal is a contract violation, not input to coerce.
    if not isinstance(value, str):
        raise TossResponseContractError(f"'{field}' must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise TossResponseContractError(f"'{field}' must be a decimal string") from exc
    if not result.is_finite():
        raise TossResponseContractError(f"'{field}' must be finite")
    return result


def _opt_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    return _req_decimal(value, field)


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


def _parse_investor_volume(raw: Any) -> TossInvestorTradingVolume:
    obj = _req_map(raw, "investorVolume")
    return TossInvestorTradingVolume(
        buy_volume=_req_decimal(obj["buyVolume"], "buyVolume"),
        sell_volume=_req_decimal(obj["sellVolume"], "sellVolume"),
        net_buy_volume=_req_decimal(obj["netBuyVolume"], "netBuyVolume"),
    )


def _parse_optional_investor_volume(raw: Any) -> TossInvestorTradingVolume | None:
    obj = _opt_map(raw, "investorVolume")
    return None if obj is None else _parse_investor_volume(obj)


def _parse_stock_institution_breakdown(raw: Any) -> TossStockInstitutionBreakdown:
    obj = _req_map(raw, "breakdown")
    return TossStockInstitutionBreakdown(
        financial_investment=_parse_investor_volume(obj["financialInvestment"]),
        insurance=_parse_investor_volume(obj["insurance"]),
        trust=_parse_investor_volume(obj["trust"]),
        private_equity_fund=_parse_investor_volume(obj["privateEquityFund"]),
        bank=_parse_investor_volume(obj["bank"]),
        other_financial_institution=_parse_investor_volume(
            obj["otherFinancialInstitution"]
        ),
        pension_fund=_parse_investor_volume(obj["pensionFund"]),
    )


def _parse_stock_institution_trading(raw: Any) -> TossStockInstitutionTrading:
    obj = _req_map(raw, "institution")
    breakdown_raw = _opt_map(obj.get("breakdown"), "institution.breakdown")
    return TossStockInstitutionTrading(
        buy_volume=_req_decimal(obj["buyVolume"], "institution.buyVolume"),
        sell_volume=_req_decimal(obj["sellVolume"], "institution.sellVolume"),
        net_buy_volume=_req_decimal(obj["netBuyVolume"], "institution.netBuyVolume"),
        breakdown=(
            None
            if breakdown_raw is None
            else _parse_stock_institution_breakdown(breakdown_raw)
        ),
    )


def _parse_foreigner_holding(raw: Any) -> TossForeignerHolding | None:
    obj = _opt_map(raw, "foreignerHolding")
    if obj is None:
        return None
    return TossForeignerHolding(
        holding_quantity=_req_decimal(obj["holdingQuantity"], "holdingQuantity"),
        limit_quantity=_req_decimal(obj["limitQuantity"], "limitQuantity"),
        holding_rate=_req_decimal(obj["holdingRate"], "holdingRate"),
    )


def _parse_cfd_balance(raw: Any) -> TossCfdBalance | None:
    obj = _opt_map(raw, "cfd")
    if obj is None:
        return None
    return TossCfdBalance(
        buy_balance_quantity=_req_decimal(
            obj["buyBalanceQuantity"], "buyBalanceQuantity"
        ),
        buy_balance_rate=_req_decimal(obj["buyBalanceRate"], "buyBalanceRate"),
        sell_balance_quantity=_req_decimal(
            obj["sellBalanceQuantity"], "sellBalanceQuantity"
        ),
        sell_balance_rate=_req_decimal(obj["sellBalanceRate"], "sellBalanceRate"),
    )


def _parse_stock_investor_record(raw: Any) -> TossStockInvestorTradingRecord:
    obj = _req_map(raw, "records[]")
    return TossStockInvestorTradingRecord(
        date=_req_str(obj["date"], "date"),
        updated_at=_req_str(obj["updatedAt"], "updatedAt"),
        foreigner=_parse_investor_volume(obj["foreigner"]),
        institution=_parse_stock_institution_trading(obj["institution"]),
        individual=_parse_optional_investor_volume(obj.get("individual")),
        other_corporation=_parse_optional_investor_volume(obj.get("otherCorporation")),
        foreigner_holding=_parse_foreigner_holding(obj.get("foreignerHolding")),
        cfd=_parse_cfd_balance(obj.get("cfd")),
    )


def parse_stock_investor_trading_page(
    raw: dict[str, Any],
) -> TossStockInvestorTradingPage:
    try:
        records_raw = _req_map(raw, "response")["records"]
        if not isinstance(records_raw, list):
            raise TossResponseContractError(
                "stocks/{symbol}/investor-trading: 'records' must be a list"
            )
        records = [_parse_stock_investor_record(row) for row in records_raw]
        return TossStockInvestorTradingPage(
            records=records,
            next_until=_opt_str(raw.get("nextUntil"), "nextUntil"),
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
        if not isinstance(raw, list):
            raise TossResponseContractError(
                "market-indicators/prices: result must be a list"
            )
        return [
            TossMarketIndicatorPrice(
                symbol=_req_str(row.get("symbol"), "symbol"),
                timestamp=_opt_str(row.get("timestamp"), "timestamp"),
                last_price=_req_decimal(row.get("lastPrice"), "lastPrice"),
            )
            for row in (_req_map(item, "result[]") for item in raw)
        ]
    except TossResponseContractError:
        raise
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


def _parse_trading_amount(raw: Any) -> TossInvestorTradingAmount:
    obj = _req_map(raw, "tradingAmount")
    return TossInvestorTradingAmount(
        buy_amount=_req_decimal(obj["buyAmount"], "buyAmount"),
        sell_amount=_req_decimal(obj["sellAmount"], "sellAmount"),
    )


def _parse_market_institution_trading(raw: Any) -> TossMarketInstitutionTrading:
    obj = _req_map(raw, "institution")
    breakdown_obj = _req_map(obj["breakdown"], "institution.breakdown")
    return TossMarketInstitutionTrading(
        buy_amount=_req_decimal(obj["buyAmount"], "institution.buyAmount"),
        sell_amount=_req_decimal(obj["sellAmount"], "institution.sellAmount"),
        breakdown=TossMarketInstitutionBreakdown(
            financial_investment=_parse_trading_amount(
                breakdown_obj["financialInvestment"]
            ),
            insurance=_parse_trading_amount(breakdown_obj["insurance"]),
            trust=_parse_trading_amount(breakdown_obj["trust"]),
            private_equity_fund=_parse_trading_amount(
                breakdown_obj["privateEquityFund"]
            ),
            bank=_parse_trading_amount(breakdown_obj["bank"]),
            other_financial_institution=_parse_trading_amount(
                breakdown_obj["otherFinancialInstitution"]
            ),
            pension_fund=_parse_trading_amount(breakdown_obj["pensionFund"]),
        ),
    )


def _parse_market_investor_record(
    raw: Any,
) -> TossMarketInvestorTradingRecord:
    obj = _req_map(raw, "records[]")
    return TossMarketInvestorTradingRecord(
        date=_req_str(obj["date"], "date"),
        updated_at=_req_str(obj["updatedAt"], "updatedAt"),
        individual=_parse_trading_amount(obj["individual"]),
        foreigner=_parse_trading_amount(obj["foreigner"]),
        institution=_parse_market_institution_trading(obj["institution"]),
        other_corporation=_parse_trading_amount(obj["otherCorporation"]),
    )


def parse_market_investor_trading_page(
    raw: dict[str, Any],
) -> TossMarketInvestorTradingPage:
    try:
        records_raw = _req_map(raw, "response")["records"]
        if not isinstance(records_raw, list):
            raise TossResponseContractError(
                "market-indicators/{symbol}/investor-trading: 'records' must be a list"
            )
        records = [_parse_market_investor_record(row) for row in records_raw]
        return TossMarketInvestorTradingPage(
            records=records,
            next_until=_opt_str(raw.get("nextUntil"), "nextUntil"),
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
        rankings_raw = _req_map(raw, "response")["rankings"]
        if not isinstance(rankings_raw, list):
            raise TossResponseContractError("rankings: 'rankings' must be a list")
        items = []
        for row in rankings_raw:
            obj = _req_map(row, "rankings[]")
            price_obj = _req_map(obj["price"], "price")
            items.append(
                TossRankingItem(
                    rank=_req_int(obj["rank"], "rank"),
                    symbol=_req_str(obj["symbol"], "symbol"),
                    currency=_req_str(obj["currency"], "currency"),
                    price=TossRankingPrice(
                        last_price=_req_decimal(price_obj["lastPrice"], "lastPrice"),
                        base_price=_req_decimal(price_obj["basePrice"], "basePrice"),
                        change_rate=_opt_decimal(
                            price_obj.get("changeRate"), "changeRate"
                        ),
                    ),
                    trading_volume=_req_decimal(obj["tradingVolume"], "tradingVolume"),
                    trading_amount=_req_decimal(obj["tradingAmount"], "tradingAmount"),
                )
            )
        return TossRankings(
            rankings=items,
            ranked_at=_opt_str(raw.get("rankedAt"), "rankedAt"),
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
        if not isinstance(raw, list):
            raise TossResponseContractError("stocks/all: result must be a list")
        return [
            TossListedStock(
                symbol=_req_str(row.get("symbol"), "symbol"),
                name=_req_str(row.get("name"), "name"),
                security_type=_req_str(row.get("securityType"), "securityType"),
                is_common_share=_req_bool(row.get("isCommonShare"), "isCommonShare"),
                isin_code=_req_str(row.get("isinCode"), "isinCode"),
            )
            for row in (_req_map(item, "result[]") for item in raw)
        ]
    except TossResponseContractError:
        raise
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "stocks/all payload violates the documented schema"
        ) from exc


# ---------------------------------------------------------------------------
# #1086 strict 1-minute candle page (GET /api/v1/candles, interval=1m).
#
# Field names quote the official schemas verbatim (openapi.json v1.2.19):
#   CandlePageResponse: required ``candles`` (array of Candle, newest first),
#                       optional nullable ``nextBefore`` (date-time).
#   Candle: required timestamp (date-time), openPrice, highPrice, lowPrice,
#           closePrice, volume (decimal strings), currency.
# The spec documents ``timestamp`` for 1m as the bar END: the bar aggregates
# trades in ``[timestamp - 1 minute, timestamp)``. ``bar_start`` is derived,
# never sent by the provider.
#
# The loose ``parse_candles`` above stays as-is for its existing consumers.
# ---------------------------------------------------------------------------

MINUTE_BAR_LENGTH_SECONDS = 60


def _req_datetime(value: Any, field: str) -> datetime:
    text = _req_str(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TossResponseContractError(
            f"'{field}' must be an ISO 8601 date-time"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TossResponseContractError(f"'{field}' must carry a timezone offset")
    return parsed


@dataclass(frozen=True)
class TossMinuteCandle:
    """Candle for interval=1m. ``timestamp`` is the provider bar END time."""

    timestamp: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    currency: str

    @property
    def bar_start(self) -> datetime:
        """Derived start of ``[timestamp - 1 minute, timestamp)``."""
        return self.timestamp - timedelta(seconds=MINUTE_BAR_LENGTH_SECONDS)

    def same_values(self, other: TossMinuteCandle) -> bool:
        return (
            self.open_price == other.open_price
            and self.high_price == other.high_price
            and self.low_price == other.low_price
            and self.close_price == other.close_price
            and self.volume == other.volume
            and self.currency == other.currency
        )


@dataclass(frozen=True)
class TossMinuteCandlePage:
    candles: list[TossMinuteCandle]
    next_before: datetime | None
    #: The raw ``nextBefore`` string, passed back verbatim as ``before``.
    next_before_raw: str | None


def _parse_minute_candle(raw: Any) -> TossMinuteCandle:
    row = _req_map(raw, "candles[]")
    timestamp = _req_datetime(row["timestamp"], "timestamp")
    if timestamp.second != 0 or timestamp.microsecond != 0:
        raise TossResponseContractError(
            "candles: 1m 'timestamp' must be minute-aligned"
        )
    candle = TossMinuteCandle(
        timestamp=timestamp,
        open_price=_req_decimal(row["openPrice"], "openPrice"),
        high_price=_req_decimal(row["highPrice"], "highPrice"),
        low_price=_req_decimal(row["lowPrice"], "lowPrice"),
        close_price=_req_decimal(row["closePrice"], "closePrice"),
        volume=_req_decimal(row["volume"], "volume"),
        currency=_req_str(row["currency"], "currency"),
    )
    if (
        candle.volume < 0
        or min(
            candle.open_price, candle.high_price, candle.low_price, candle.close_price
        )
        < 0
    ):
        raise TossResponseContractError("candles: negative price or volume")
    if candle.high_price < max(
        candle.open_price, candle.close_price, candle.low_price
    ) or candle.low_price > min(candle.open_price, candle.close_price):
        raise TossResponseContractError("candles: OHLC invariant violated")
    return candle


def parse_minute_candle_page(raw: Any) -> TossMinuteCandlePage:
    try:
        body = _req_map(raw, "response")
        rows = body["candles"]
        if not isinstance(rows, list):
            raise TossResponseContractError("candles: 'candles' must be a list")
        candles = [_parse_minute_candle(row) for row in rows]
        # Official ordering: newest first. Equal timestamps are left for the
        # paginator to judge (identical duplicate vs conflicting duplicate).
        for newer, older in zip(candles, candles[1:], strict=False):
            if older.timestamp > newer.timestamp:
                raise TossResponseContractError(
                    "candles: page is not ordered newest-first"
                )
        next_before_raw = _opt_str(body.get("nextBefore"), "nextBefore")
        next_before = (
            _req_datetime(next_before_raw, "nextBefore")
            if next_before_raw is not None
            else None
        )
        return TossMinuteCandlePage(
            candles=candles,
            next_before=next_before,
            next_before_raw=next_before_raw,
        )
    except TossResponseContractError:
        raise
    except _CONTRACT_FIELDS as exc:
        raise TossResponseContractError(
            "candles payload violates the documented schema"
        ) from exc
