from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import (
    parse_candles,
    parse_decimal_string,
    parse_holdings,
    parse_orders,
    parse_prices,
    parse_stocks,
)


def test_parse_decimal_string_rejects_float() -> None:
    with pytest.raises(TypeError, match="float"):
        parse_decimal_string(1.23)


def test_parse_prices_converts_decimal_strings() -> None:
    prices = parse_prices(
        [
            {
                "symbol": "BRK.B",
                "timestamp": "2026-06-12T00:00:00Z",
                "lastPrice": "430.12",
                "currency": "USD",
            }
        ]
    )

    assert prices[0].symbol == "BRK.B"
    assert prices[0].last_price == Decimal("430.12")
    assert prices[0].currency == "USD"


def test_parse_stocks_preserves_unknown_enum_strings() -> None:
    stocks = parse_stocks(
        [
            {
                "symbol": "005930",
                "name": "삼성전자",
                "englishName": "Samsung Electronics",
                "isinCode": "KR7005930003",
                "market": "UNKNOWN_MARKET",
                "securityType": "NEW_TYPE",
                "isCommonShare": True,
                "status": "ACTIVE",
                "currency": "KRW",
                "listDate": "1975-06-11",
                "delistDate": None,
                "sharesOutstanding": "5841240000",
                "leverageFactor": None,
                "koreanMarketDetail": {"nxtSupported": True},
            }
        ]
    )

    assert stocks[0].security_type == "NEW_TYPE"
    assert stocks[0].shares_outstanding == Decimal("5841240000")


def test_parse_holdings_converts_nested_decimal_strings() -> None:
    holdings = parse_holdings(
        {
            "items": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "10",
                    "lastPrice": "70000",
                    "averagePurchasePrice": "65000",
                    "marketValue": {
                        "purchaseAmount": "650000",
                        "amount": "700000",
                        "amountAfterCost": "699000",
                    },
                    "profitLoss": {"amount": "50000", "rate": "0.0769"},
                    "dailyProfitLoss": {"amount": "1000", "rate": "0.0014"},
                    "cost": {"commission": "0", "tax": "0"},
                }
            ]
        }
    )

    assert holdings.items[0].quantity == Decimal("10")
    assert holdings.items[0].market_value["amount"] == Decimal("700000")


def test_parse_orders_converts_execution_decimals() -> None:
    orders = parse_orders(
        {
            "orders": [
                {
                    "orderId": "ord-1",
                    "clientOrderId": "tosprop-legacy-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "orderType": "LIMIT",
                    "timeInForce": "DAY",
                    "status": "FUTURE_STATUS",
                    "price": "190.00",
                    "quantity": "1.5",
                    "orderAmount": None,
                    "currency": "USD",
                    "orderedAt": "2026-06-12T00:00:00Z",
                    "canceledAt": None,
                    "execution": {
                        "filledQuantity": "0.5",
                        "averageFilledPrice": "189.50",
                        "filledAmount": "94.75",
                        "commission": "0.10",
                        "tax": None,
                        "filledAt": "2026-06-12T00:01:00Z",
                        "settlementDate": "2026-06-14",
                    },
                }
            ],
            "nextCursor": None,
            "hasNext": False,
        }
    )

    assert orders.orders[0].status == "FUTURE_STATUS"
    assert orders.orders[0].client_order_id == "tosprop-legacy-1"
    assert orders.orders[0].execution["commission"] == Decimal("0.10")


def test_parse_candles_converts_decimal_strings_and_cursor() -> None:
    page = parse_candles(
        {
            "candles": [
                {
                    "timestamp": "2026-06-12T09:14:00.000+09:00",
                    "openPrice": "330250",
                    "highPrice": "330500",
                    "lowPrice": "330000",
                    "closePrice": "330500",
                    "volume": "10276",
                    "currency": "KRW",
                }
            ],
            "nextBefore": "2026-06-12T09:13:00.000+09:00",
        }
    )

    assert page.next_before == "2026-06-12T09:13:00.000+09:00"
    assert page.candles[0].timestamp == "2026-06-12T09:14:00.000+09:00"
    assert page.candles[0].open_price == Decimal("330250")
    assert page.candles[0].close_price == Decimal("330500")
    assert page.candles[0].volume == Decimal("10276")
    assert page.candles[0].currency == "KRW"


def test_parse_candles_rejects_float_prices() -> None:
    with pytest.raises(TypeError, match="float"):
        parse_candles(
            {
                "candles": [
                    {
                        "timestamp": "2026-06-12T09:14:00.000+09:00",
                        "openPrice": 330250.0,
                        "highPrice": "330500",
                        "lowPrice": "330000",
                        "closePrice": "330500",
                        "volume": "10276",
                        "currency": "KRW",
                    }
                ],
                "nextBefore": None,
            }
        )


def test_parse_warnings_converts_fields() -> None:
    from app.services.brokers.toss.dto import parse_warnings

    warnings = parse_warnings(
        [
            {
                "warningType": "OVERHEATED",
                "exchange": "KRX",
                "startDate": "2026-03-20",
                "endDate": "2026-03-27",
            },
            {
                "warningType": "VI_STATIC",
                "exchange": None,
                "startDate": "2026-03-26",
                "endDate": None,
            },
        ]
    )

    assert len(warnings) == 2
    assert warnings[0].warning_type == "OVERHEATED"
    assert warnings[0].exchange == "KRX"
    assert warnings[0].start_date == "2026-03-20"
    assert warnings[0].end_date == "2026-03-27"

    assert warnings[1].warning_type == "VI_STATIC"
    assert warnings[1].exchange is None
    assert warnings[1].start_date == "2026-03-26"
    assert warnings[1].end_date is None
