from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.brokers.toss.dto import (
    TossBuyingPower,
    TossHoldingItem,
    TossHoldings,
    TossSellableQuantity,
)
from app.services.toss_portfolio_service import fetch_toss_portfolio_snapshot


def _holding(
    *,
    symbol: str = "BRK.B",
    market_country: str = "US",
    currency: str = "USD",
    quantity: str = "1.5",
    sellable: str = "1.25",
) -> TossHoldingItem:
    return TossHoldingItem(
        symbol=symbol,
        name="Berkshire Hathaway B",
        market_country=market_country,
        currency=currency,
        quantity=Decimal(quantity),
        last_price=Decimal("430.12"),
        average_purchase_price=Decimal("400.00"),
        market_value={
            "amount": Decimal("645.18"),
            "amountAfterCost": Decimal("644.50"),
        },
        profit_loss={"amount": Decimal("45.18"), "rate": Decimal("0.0753")},
        daily_profit_loss={"amount": Decimal("1.20"), "rate": Decimal("0.0019")},
        cost={"commission": Decimal("0.68"), "tax": Decimal("0")},
    )


class _FakeTossClient:
    def __init__(self) -> None:
        self.closed = False
        self.sellable_calls: list[str] = []
        self.buying_power_calls: list[str] = []

    async def holdings(self) -> TossHoldings:
        return TossHoldings(items=[_holding()])

    async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
        self.sellable_calls.append(symbol)
        return TossSellableQuantity(sellable_quantity=Decimal("1.25"))

    async def buying_power(self, *, currency: str) -> TossBuyingPower:
        self.buying_power_calls.append(currency)
        amount = Decimal("123456") if currency == "KRW" else Decimal("789.01")
        return TossBuyingPower(currency=currency, cash_buying_power=amount)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_maps_holdings_sellable_and_cash() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    assert client.closed is False
    assert client.sellable_calls == ["BRK.B"]
    assert client.buying_power_calls == ["KRW", "USD"]
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.cash_usd == Decimal("789.01")
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.symbol == "BRK.B"
    assert position.instrument_type == "equity_us"
    assert position.market == "us"
    assert position.quantity == Decimal("1.5")
    assert position.sellable_quantity == Decimal("1.25")
    assert position.evaluation_amount == Decimal("645.18")
    assert position.profit_rate == Decimal("0.0753")


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_keeps_position_when_sellable_fails() -> None:
    class Client(_FakeTossClient):
        async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
            raise RuntimeError(f"sellable failed for {symbol}")

    snapshot = await fetch_toss_portfolio_snapshot(client=Client())

    assert snapshot.positions[0].sellable_quantity is None
    assert snapshot.errors == [
        {
            "source": "toss_api",
            "stage": "sellable_quantity",
            "symbol": "BRK.B",
            "error": "sellable failed for BRK.B",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_maps_kr_market() -> None:
    class Client(_FakeTossClient):
        async def holdings(self) -> TossHoldings:
            return TossHoldings(
                items=[
                    _holding(
                        symbol="005930",
                        market_country="KR",
                        currency="KRW",
                        quantity="10",
                    )
                ]
            )

    snapshot = await fetch_toss_portfolio_snapshot(client=Client())

    assert snapshot.positions[0].symbol == "005930"
    assert snapshot.positions[0].instrument_type == "equity_kr"
    assert snapshot.positions[0].market == "kr"
