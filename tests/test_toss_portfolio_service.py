from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from app.services.brokers.toss.dto import (
    TossBuyingPower,
    TossHoldingItem,
    TossHoldings,
    TossSellableQuantity,
)
from app.services.toss_portfolio_service import (
    fetch_toss_cash_snapshot,
    fetch_toss_portfolio_snapshot,
)
from app.services.toss_sellable_cache import TossSellableCache


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
async def test_fetch_toss_portfolio_snapshot_keeps_position_when_sellable_fails() -> (
    None
):
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


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_skips_sellable_when_not_needed() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client, need_sellable=False)

    # ROB-685: the ORDER_INFO N+1 fanout is skipped entirely.
    assert client.sellable_calls == []
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].sellable_quantity is None
    # Holdings + cash still resolve normally.
    assert snapshot.positions[0].symbol == "BRK.B"
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.errors == []


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_default_still_fetches_sellable() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    # Default is unchanged: sellable is fetched and mapped.
    assert client.sellable_calls == ["BRK.B"]
    assert snapshot.positions[0].sellable_quantity == Decimal("1.25")


@pytest.mark.asyncio
async def test_fetch_toss_cash_snapshot_does_not_fetch_holdings_or_sellable() -> None:
    class Client(_FakeTossClient):
        async def holdings(self) -> TossHoldings:
            raise AssertionError("cash-only path must not fetch holdings")

        async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
            raise AssertionError("cash-only path must not fetch sellable quantity")

    client = Client()

    snapshot = await fetch_toss_cash_snapshot(client=client)

    assert client.buying_power_calls == ["KRW", "USD"]
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.cash_usd == Decimal("789.01")
    assert snapshot.errors == []


@pytest.fixture
def sellable_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_snapshot_cache_hit_issues_zero_sellable_calls(sellable_redis) -> None:
    cache = TossSellableCache(ttl_seconds=600, redis_client=sellable_redis)
    client = _FakeTossClient()

    # Cold load: miss on every symbol => one fanout, cache populated.
    snap1 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B"]
    assert snap1.positions[0].sellable_quantity == Decimal("1.25")

    # Warm load within TTL: ZERO new sellable calls, value served from cache.
    snap2 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B"]  # unchanged => cache hit
    assert snap2.positions[0].sellable_quantity == Decimal("1.25")  # accuracy preserved


@pytest.mark.asyncio
async def test_snapshot_cache_refetches_after_redis_expiry(sellable_redis) -> None:
    cache = TossSellableCache(ttl_seconds=600, redis_client=sellable_redis)
    client = _FakeTossClient()

    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    await sellable_redis.delete("toss:sellable:v1:BRK.B")
    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B", "BRK.B"]  # refetched after expiry


@pytest.mark.asyncio
async def test_snapshot_cache_does_not_store_failed_fetch(sellable_redis) -> None:
    class ErrClient(_FakeTossClient):
        async def sellable_quantity(self, *, symbol: str):
            self.sellable_calls.append(symbol)
            raise RuntimeError(f"boom {symbol}")

    cache = TossSellableCache(ttl_seconds=600, redis_client=sellable_redis)
    client = ErrClient()

    snap1 = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert snap1.positions[0].sellable_quantity is None
    assert snap1.errors[0]["stage"] == "sellable_quantity"

    # Error was NOT cached => next load within TTL retries the fetch.
    await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=True, sellable_cache=cache
    )
    assert client.sellable_calls == ["BRK.B", "BRK.B"]


@pytest.mark.asyncio
async def test_snapshot_no_cache_default_still_fans_out() -> None:
    client = _FakeTossClient()
    # sellable_cache defaults to None => today's fanout path, unchanged.
    await fetch_toss_portfolio_snapshot(client=client, need_sellable=True)
    await fetch_toss_portfolio_snapshot(client=client, need_sellable=True)
    assert client.sellable_calls == ["BRK.B", "BRK.B"]


@pytest.mark.asyncio
async def test_snapshot_need_sellable_false_ignores_cache(
    sellable_redis, mocker
) -> None:
    cache = TossSellableCache(ttl_seconds=600, redis_client=sellable_redis)
    mget = mocker.spy(sellable_redis, "mget")
    client = _FakeTossClient()
    snap = await fetch_toss_portfolio_snapshot(
        client=client, need_sellable=False, sellable_cache=cache
    )
    # ROB-685 skip path is untouched: no fanout, no cache read/write.
    assert client.sellable_calls == []
    assert mget.call_count == 0
    assert snap.positions[0].sellable_quantity is None


@pytest.mark.asyncio
async def test_fill_invalidation_refetches_only_affected_symbol(
    sellable_redis,
) -> None:
    class Client(_FakeTossClient):
        async def holdings(self) -> TossHoldings:
            return TossHoldings(
                items=[
                    _holding(symbol="AAA", quantity="3"),
                    _holding(symbol="BBB", quantity="5"),
                ]
            )

        async def sellable_quantity(self, *, symbol: str) -> TossSellableQuantity:
            self.sellable_calls.append(symbol)
            assert symbol == "AAA"
            return TossSellableQuantity(sellable_quantity=Decimal("2"))

    cache = TossSellableCache(ttl_seconds=600, redis_client=sellable_redis)
    await cache.put_many({"AAA": Decimal("3"), "BBB": Decimal("5")})
    await cache.invalidate("AAA")
    client = Client()

    snapshot = await fetch_toss_portfolio_snapshot(
        client=client,
        need_sellable=True,
        need_cash=False,
        sellable_cache=cache,
    )

    assert client.sellable_calls == ["AAA"]
    assert [position.sellable_quantity for position in snapshot.positions] == [
        Decimal("2"),
        Decimal("5"),
    ]
    assert await cache.get("AAA") == Decimal("2")
    assert await cache.get("BBB") == Decimal("5")


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_skips_cash_when_not_needed() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client, need_cash=False)

    # ROB-810: the ACCOUNT-limited buying_power fanout is skipped entirely.
    assert client.buying_power_calls == []
    assert snapshot.cash_krw is None
    assert snapshot.cash_usd is None
    # Holdings + sellable still resolve normally.
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "BRK.B"
    assert snapshot.positions[0].sellable_quantity == Decimal("1.25")
    assert snapshot.errors == []


@pytest.mark.asyncio
async def test_fetch_toss_portfolio_snapshot_default_still_fetches_cash() -> None:
    client = _FakeTossClient()

    snapshot = await fetch_toss_portfolio_snapshot(client=client)

    # Default unchanged: buying_power still fetched (invest_home regression guard).
    assert client.buying_power_calls == ["KRW", "USD"]
    assert snapshot.cash_krw == Decimal("123456")
    assert snapshot.cash_usd == Decimal("789.01")
