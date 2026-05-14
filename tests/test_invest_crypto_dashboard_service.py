from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.pending_order import PendingOrder
from app.services.invest_view_model.crypto_dashboard_service import (
    build_crypto_dashboard,
)
from app.services.invest_view_model.relation_resolver import RelationResolver


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarRows(self._rows)


class FakeSession:
    def __init__(self, universe_rows, pending_rows=None):
        self.universe_rows = universe_rows
        self.pending_rows = pending_rows or []
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _Result(self.universe_rows)
        return _Result(self.pending_rows)


def _market(market="KRW-BTC", base="BTC", korean_name="비트코인"):
    return SimpleNamespace(
        market=market,
        base_currency=base,
        quote_currency="KRW",
        korean_name=korean_name,
        english_name="Bitcoin",
        is_active=True,
    )


def _pending(symbol="KRW-BTC"):
    row = PendingOrder()
    row.user_id = 7
    row.symbol = symbol
    row.market = "crypto"
    row.venue = "upbit"
    row.broker_order_id = "upbit-open-1"
    row.side = "buy"
    row.order_type = "limit"
    row.price = 100000000
    row.quantity = 0.01
    row.filled_quantity = 0
    row.status = "open"
    row.ordered_at = datetime(2026, 5, 13, 12, tzinfo=UTC)
    row.updated_at = datetime(2026, 5, 13, 12, 1, tzinfo=UTC)
    return row


@pytest.mark.asyncio
async def test_crypto_dashboard_maps_ticker_spread_relation_and_pending_orders():
    async def ticker_provider(markets):
        assert markets == ["KRW-BTC"]
        return [
            {
                "market": "KRW-BTC",
                "trade_price": 101000000,
                "signed_change_rate": 0.0123,
                "signed_change_price": 1230000,
                "acc_trade_price_24h": 12345678900,
                "acc_trade_volume_24h": 234.5,
            }
        ]

    async def spread_provider(markets):
        assert markets == ["KRW-BTC"]
        return {"KRW-BTC": 0.62}

    resolver = RelationResolver(
        held={("crypto", "KRW-BTC")}, watch={("crypto", "KRW-BTC")}
    )
    response = await build_crypto_dashboard(
        db=FakeSession([_market()], [_pending("BTC")]),
        user_id=7,
        resolver=resolver,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
    )

    assert response.market == "crypto"
    assert response.cards[0].symbol == "KRW-BTC"
    assert response.cards[0].priceKrw == 101000000
    assert response.cards[0].orderbookSpreadPct == pytest.approx(0.62)
    assert response.cards[0].isHeld is True
    assert response.cards[0].isWatched is True
    assert {badge.kind for badge in response.cards[0].badges} >= {
        "held",
        "pending_order",
        "thin_orderbook",
    }
    assert response.pendingOrders is not None
    assert response.pendingOrders.items[0].orderId == "upbit-open-1"
    assert response.pendingOrders.emptyState is None
    assert response.capabilities.execution.state == "read_only_mvp"


@pytest.mark.asyncio
async def test_crypto_dashboard_is_renderable_when_public_sources_fail():
    async def failing_ticker(_markets):
        raise RuntimeError("upstream down")

    async def failing_spread(_markets):
        raise RuntimeError("upstream down")

    response = await build_crypto_dashboard(
        db=FakeSession([_market()], []),
        user_id=7,
        ticker_provider=failing_ticker,
        orderbook_spread_provider=failing_spread,
    )

    assert response.cards[0].priceKrw is None
    assert response.pendingOrders is not None
    assert response.pendingOrders.emptyState == "no_pending_orders"
    assert "crypto_ticker_unavailable" in response.meta.warnings
    assert "crypto_orderbook_unavailable" in response.meta.warnings
    assert {source.state for source in response.meta.sources} == {"unavailable"}


@pytest.mark.asyncio
async def test_dashboard_records_stale_source_state_from_read_model():
    from app.services.upbit_public_read_model.types import (
        UpbitBlockMeta,
        UpbitTickerBlock,
    )

    async def stale_ticker_provider(markets):
        return UpbitTickerBlock(
            meta=UpbitBlockMeta(
                source="upbit_ticker",
                state="stale",
                label="Upbit ticker",
                errorReason="rate_limited",
            ),
            tickers={m: {"market": m, "trade_price": 100.0} for m in markets},
        )

    response = await build_crypto_dashboard(
        db=FakeSession([_market()], []),
        user_id=7,
        ticker_provider=stale_ticker_provider,
        orderbook_spread_provider=lambda _: {},
    )
    sources = {source.source: source for source in response.meta.sources}
    assert sources["upbit_ticker"].state == "supported"
    assert response.cards[0].priceKrw == 100.0


@pytest.mark.asyncio
async def test_default_dashboard_reuses_single_upbit_read_model_redis_client(
    monkeypatch,
):
    import fakeredis.aioredis

    from app.services.upbit_public_read_model import close_default_read_model

    await close_default_read_model()
    redis_clients = []

    async def create_redis_client():
        client = fakeredis.aioredis.FakeRedis(decode_responses=False)
        redis_clients.append(client)
        return client

    async def fetch_tickers(markets):
        return [
            {
                "market": market,
                "trade_price": 101000000,
                "signed_change_rate": 0.0123,
                "signed_change_price": 1230000,
                "acc_trade_price_24h": 12345678900,
                "acc_trade_volume_24h": 234.5,
            }
            for market in markets
        ]

    async def fetch_orderbooks(markets):
        return {
            market: {
                "market": market,
                "orderbook_units": [
                    {
                        "ask_price": 101000000,
                        "bid_price": 100900000,
                        "ask_size": 1,
                        "bid_size": 1,
                    }
                ],
            }
            for market in markets
        }

    async def fetch_trades(_market, _count):
        return []

    monkeypatch.setattr(
        "app.services.ohlcv_cache_common.create_redis_client", create_redis_client
    )
    monkeypatch.setattr(
        "app.services.brokers.upbit.client.fetch_multiple_tickers", fetch_tickers
    )
    monkeypatch.setattr(
        "app.services.upbit_orderbook.fetch_multiple_orderbooks", fetch_orderbooks
    )
    monkeypatch.setattr(
        "app.services.brokers.upbit.public_trades.fetch_recent_trades", fetch_trades
    )

    try:
        first = await build_crypto_dashboard(db=FakeSession([_market()], []), user_id=7)
        second = await build_crypto_dashboard(
            db=FakeSession([_market()], []), user_id=7
        )

        assert len(redis_clients) == 1
        assert first.cards[0].priceKrw == 101000000
        assert second.cards[0].priceKrw == 101000000
        assert {source.source for source in first.meta.sources} >= {
            "upbit_ticker",
            "upbit_orderbook",
        }
        assert first.meta.warnings == []
    finally:
        await close_default_read_model()
