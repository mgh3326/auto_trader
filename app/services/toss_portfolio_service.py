from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.services.brokers.toss.client import TossReadClient


class TossPortfolioClient(Protocol):
    async def holdings(self) -> Any: ...
    async def sellable_quantity(self, *, symbol: str) -> Any: ...
    async def buying_power(self, *, currency: str) -> Any: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TossPortfolioPosition:
    account: str
    account_name: str
    broker: str
    source: str
    instrument_type: str
    market: str
    symbol: str
    name: str
    quantity: Decimal
    avg_buy_price: Decimal
    current_price: Decimal
    evaluation_amount: Decimal | None
    profit_loss: Decimal | None
    profit_rate: Decimal | None
    sellable_quantity: Decimal | None


@dataclass(frozen=True)
class TossPortfolioSnapshot:
    positions: list[TossPortfolioPosition]
    cash_krw: Decimal | None = None
    cash_usd: Decimal | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


def _instrument_type_for_market_country(market_country: str) -> str:
    normalized = market_country.strip().upper()
    if normalized == "KR":
        return "equity_kr"
    if normalized == "US":
        return "equity_us"
    raise ValueError(f"Unsupported Toss marketCountry: {market_country}")


def _market_for_instrument_type(instrument_type: str) -> str:
    if instrument_type == "equity_kr":
        return "kr"
    if instrument_type == "equity_us":
        return "us"
    raise ValueError(f"Unsupported Toss instrument_type: {instrument_type}")


def _decimal_dict_value(raw: dict[str, Any], key: str) -> Decimal | None:
    value = raw.get(key)
    return value if isinstance(value, Decimal) else None


async def fetch_toss_portfolio_snapshot(
    *,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    try:
        holdings = await active_client.holdings()
        errors: list[dict[str, Any]] = []

        sellable_results = await asyncio.gather(
            *[
                active_client.sellable_quantity(symbol=item.symbol)
                for item in holdings.items
            ],
            return_exceptions=True,
        )

        positions: list[TossPortfolioPosition] = []
        for item, sellable_result in zip(holdings.items, sellable_results, strict=True):
            sellable_quantity: Decimal | None = None
            if isinstance(sellable_result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "sellable_quantity",
                        "symbol": item.symbol,
                        "error": str(sellable_result),
                    }
                )
            else:
                sellable_quantity = sellable_result.sellable_quantity

            instrument_type = _instrument_type_for_market_country(item.market_country)
            positions.append(
                TossPortfolioPosition(
                    account="toss",
                    account_name="Toss",
                    broker="toss",
                    source="toss_api",
                    instrument_type=instrument_type,
                    market=_market_for_instrument_type(instrument_type),
                    symbol=item.symbol.strip().upper(),
                    name=item.name or item.symbol,
                    quantity=item.quantity,
                    avg_buy_price=item.average_purchase_price,
                    current_price=item.last_price,
                    evaluation_amount=_decimal_dict_value(item.market_value, "amount"),
                    profit_loss=_decimal_dict_value(item.profit_loss, "amount"),
                    profit_rate=_decimal_dict_value(item.profit_loss, "rate"),
                    sellable_quantity=sellable_quantity,
                )
            )

        buying_power_results = await asyncio.gather(
            active_client.buying_power(currency="KRW"),
            active_client.buying_power(currency="USD"),
            return_exceptions=True,
        )
        cash_krw: Decimal | None = None
        cash_usd: Decimal | None = None
        for currency, result in zip(("KRW", "USD"), buying_power_results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    {
                        "source": "toss_api",
                        "stage": "buying_power",
                        "currency": currency,
                        "error": str(result),
                    }
                )
                continue
            if result.currency == "KRW":
                cash_krw = result.cash_buying_power
            elif result.currency == "USD":
                cash_usd = result.cash_buying_power

        return TossPortfolioSnapshot(
            positions=positions,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        if created_client:
            await active_client.aclose()
