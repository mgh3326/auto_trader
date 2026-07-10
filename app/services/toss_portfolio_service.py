from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import sentry_sdk

from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossSellableQuantity
from app.services.toss_sellable_cache import TossSellableCache


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


@dataclass(frozen=True)
class TossCashSnapshot:
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


async def fetch_toss_cash_snapshot(
    *,
    client: TossPortfolioClient | None = None,
) -> TossCashSnapshot:
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    try:
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.buying_power",
        ) as span:
            span.set_data("currency_count", 2)
            buying_power_results = await asyncio.gather(
                active_client.buying_power(currency="KRW"),
                active_client.buying_power(currency="USD"),
                return_exceptions=True,
            )
            span.set_data(
                "error_count",
                sum(
                    1
                    for result in buying_power_results
                    if isinstance(result, BaseException)
                ),
            )
        cash_krw: Decimal | None = None
        cash_usd: Decimal | None = None
        errors: list[dict[str, Any]] = []
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

        return TossCashSnapshot(
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        if created_client:
            await active_client.aclose()


async def fetch_toss_portfolio_snapshot(
    *,
    need_sellable: bool = True,
    need_cash: bool = True,
    sellable_cache: TossSellableCache | None = None,
    client: TossPortfolioClient | None = None,
) -> TossPortfolioSnapshot:
    created_client = client is None
    active_client: TossPortfolioClient = client or TossReadClient.from_settings()

    # ROB-707: the cash (buying-power) snapshot is independent of holdings, so
    # kick it off concurrently with the holdings/sellable chain instead of
    # awaiting it serially after the position loop. Output is unchanged; only
    # the wall-clock overlap changes. Drained/cancelled in the finally if the
    # holdings chain raises before we await it.
    # ROB-810: callers that discard cash (MCP get_holdings) pass need_cash=False
    # so the ACCOUNT 1-TPS buying_power fanout (~3.1s) is skipped entirely.
    cash_task: asyncio.Future | None = (
        asyncio.ensure_future(fetch_toss_cash_snapshot(client=active_client))
        if need_cash
        else None
    )

    try:
        with sentry_sdk.start_span(
            op="invest.home.toss_api.phase",
            name="invest.home.toss_api.holdings",
        ) as span:
            holdings = await active_client.holdings()
            span.set_data("position_count", len(holdings.items))

        errors: list[dict[str, Any]] = []

        if need_sellable and sellable_cache is not None:
            # ROB-701: only cache-MISS symbols hit the ORDER_INFO (6 TPS)
            # /sellable-quantity endpoint; hits reuse the cached value. Re-wrap
            # hits as TossSellableQuantity so the position-build loop below is
            # unchanged.
            hits: list[Decimal | None] = [
                sellable_cache.get(item.symbol) for item in holdings.items
            ]
            miss_indices = [i for i, hit in enumerate(hits) if hit is None]
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                span.set_data("cache_miss_count", len(miss_indices))
                fetched = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=holdings.items[i].symbol)
                        for i in miss_indices
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(1 for result in fetched if isinstance(result, BaseException)),
                )
            fetched_by_index: dict[int, Any] = dict(
                zip(miss_indices, fetched, strict=True)
            )
            for index, result in fetched_by_index.items():
                if not isinstance(result, BaseException):
                    # Cache ONLY successful fetches — a transient error must not
                    # poison the cache (next load retries).
                    sellable_cache.put(
                        holdings.items[index].symbol, result.sellable_quantity
                    )
            paired: list[tuple[Any, Any]] = []
            for index, item in enumerate(holdings.items):
                if index in fetched_by_index:
                    paired.append((item, fetched_by_index[index]))
                else:
                    paired.append(
                        (item, TossSellableQuantity(sellable_quantity=hits[index]))
                    )
        elif need_sellable:
            with sentry_sdk.start_span(
                op="invest.home.toss_api.phase",
                name="invest.home.toss_api.sellable_quantity",
            ) as span:
                span.set_data("position_count", len(holdings.items))
                sellable_results = await asyncio.gather(
                    *[
                        active_client.sellable_quantity(symbol=item.symbol)
                        for item in holdings.items
                    ],
                    return_exceptions=True,
                )
                span.set_data(
                    "error_count",
                    sum(
                        1
                        for result in sellable_results
                        if isinstance(result, BaseException)
                    ),
                )
            paired = list(zip(holdings.items, sellable_results, strict=True))
        else:
            # ROB-685: caller does not consume sellable_quantity — skip the
            # per-holding GET /sellable-quantity (ORDER_INFO, 6 TPS) fanout that
            # otherwise serializes to ~6/sec and dominates wall time.
            paired = [(item, None) for item in holdings.items]

        positions: list[TossPortfolioPosition] = []
        for item, sellable_result in paired:
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
            elif sellable_result is not None:
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

        if cash_task is not None:
            cash_snapshot = await cash_task
            errors.extend(cash_snapshot.errors)
            cash_krw = cash_snapshot.cash_krw
            cash_usd = cash_snapshot.cash_usd
        else:
            cash_krw = None
            cash_usd = None

        return TossPortfolioSnapshot(
            positions=positions,
            cash_krw=cash_krw,
            cash_usd=cash_usd,
            errors=errors,
        )
    finally:
        # ROB-707: if the holdings/sellable chain raised before we awaited the
        # cash task, cancel and drain it so it never touches a closed client
        # (and never leaks a pending task). fetch_toss_cash_snapshot swallows
        # per-currency errors internally, so this only fires on holdings-chain
        # failure.
        if cash_task is not None and not cash_task.done():
            cash_task.cancel()
            with contextlib.suppress(BaseException):
                await cash_task
        if created_client:
            await active_client.aclose()
