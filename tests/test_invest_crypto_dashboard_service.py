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
        english_name=base,
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
        assert markets == ["KRW-BTC", "KRW-ETH"]
        return [
            {
                "market": "KRW-BTC",
                "trade_price": 101000000,
                "signed_change_rate": 0.041,
                "signed_change_price": 1230000,
                "acc_trade_price_24h": 12345678900,
                "acc_trade_volume_24h": 234.5,
            },
            {
                "market": "KRW-ETH",
                "trade_price": 5200000,
                "signed_change_rate": 0.032,
                "signed_change_price": 260000,
                "acc_trade_price_24h": 8500000000,
                "acc_trade_volume_24h": 1000,
            },
        ]

    async def spread_provider(markets):
        assert markets == ["KRW-BTC", "KRW-ETH"]
        return {"KRW-BTC": 0.62, "KRW-ETH": 0.2}

    resolver = RelationResolver(
        held={("crypto", "KRW-BTC")}, watch={("crypto", "KRW-ETH")}
    )
    response = await build_crypto_dashboard(
        db=FakeSession(
            [_market(), _market("KRW-ETH", "ETH", "이더리움")], [_pending("BTC")]
        ),
        user_id=7,
        resolver=resolver,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
    )

    btc = response.cards[0]
    assert response.market == "crypto"
    assert btc.symbol == "KRW-BTC"
    assert btc.priceKrw == 101000000
    assert btc.orderbookSpreadPct == pytest.approx(0.62)
    assert btc.isHeld is True
    assert btc.isWatched is False
    assert {badge.kind for badge in btc.badges} >= {
        "held",
        "pending_order",
        "thin_orderbook",
    }
    assert btc.risk is not None
    assert btc.risk.score == 35
    assert btc.risk.level == "medium"
    assert "호가 스프레드 확대" in btc.risk.reasons
    assert "미체결 상태 존재" in btc.risk.reasons
    assert response.pendingOrders is not None
    assert response.pendingOrders.items[0].orderId == "upbit-open-1"
    assert response.pendingOrders.emptyState is None
    assert response.capabilities.execution.state == "read_only_mvp"
    assert [candidate.symbol for candidate in response.insights.candidates][:2] == [
        "KRW-ETH",
        "KRW-BTC",
    ]
    assert response.insights.candidates[0].reasons[:2] == ["watched", "liquidity"]
    assert {source.source for source in response.meta.sources} >= {
        "upbit_ticker",
        "upbit_orderbook",
        "pending_orders",
        "mcp_risk_reference",
        "mcp_candidate_reference",
    }


@pytest.mark.asyncio
async def test_crypto_dashboard_marks_high_volatility_and_low_liquidity():
    async def ticker_provider(_markets):
        return [
            {
                "market": "KRW-XRP",
                "trade_price": 1000,
                "signed_change_rate": 0.081,
                "signed_change_price": 81,
                "acc_trade_price_24h": 100_000_000,
                "acc_trade_volume_24h": 10_000,
            }
        ]

    async def spread_provider(_markets):
        return {"KRW-XRP": 0.1}

    response = await build_crypto_dashboard(
        db=FakeSession([_market("KRW-XRP", "XRP", "리플")], []),
        user_id=7,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
    )

    card = response.cards[0]
    assert {badge.kind for badge in card.badges} >= {
        "high_volatility",
        "low_liquidity",
    }
    assert card.risk is not None
    assert card.risk.level == "medium"
    assert "24시간 변동성 확대" in card.risk.reasons
    candidate_summaries = " ".join(
        candidate.summary for candidate in response.insights.candidates
    )
    assert "매수" not in candidate_summaries
    assert "주문" not in candidate_summaries


@pytest.mark.asyncio
async def test_crypto_dashboard_candidate_insights_are_read_only_and_ranked():
    markets = [
        _market("KRW-AAA", "AAA", "에이"),
        _market("KRW-BBB", "BBB", "비"),
        _market("KRW-CCC", "CCC", "씨"),
        _market("KRW-DDD", "DDD", "디"),
        _market("KRW-EEE", "EEE", "이"),
        _market("KRW-FFF", "FFF", "에프"),
    ]

    async def ticker_provider(_markets):
        return [
            {
                "market": row.market,
                "trade_price": 1000,
                "signed_change_rate": 0.05 if row.market != "KRW-CCC" else 0.08,
                "signed_change_price": 50,
                "acc_trade_price_24h": 2_000_000_000 + index,
                "acc_trade_volume_24h": 10_000,
            }
            for index, row in enumerate(markets)
        ]

    async def spread_provider(_markets):
        return {row.market: (0.2 if row.market != "KRW-CCC" else 0.8) for row in markets}

    resolver = RelationResolver(watch={("crypto", "KRW-AAA"), ("crypto", "KRW-BBB")})
    response = await build_crypto_dashboard(
        db=FakeSession(markets, [_pending("BBB")]),
        user_id=7,
        resolver=resolver,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
        limit=6,
        orderbook_limit=6,
    )

    candidates = response.insights.candidates
    assert len(candidates) == 5
    assert candidates[0].symbol == "KRW-AAA"
    assert candidates[0].score > candidates[1].score
    assert any(candidate.symbol == "KRW-BBB" for candidate in candidates)
    bbb = next(candidate for candidate in candidates if candidate.symbol == "KRW-BBB")
    assert bbb.hasPendingOrder is True
    assert "pending_order" in bbb.reasons
    serialized = [candidate.model_dump() for candidate in candidates]
    forbidden_fields = {"action", "execute", "order", "watchIntent", "clientOrderId", "mutation"}
    for item in serialized:
        assert forbidden_fields.isdisjoint(item)


@pytest.mark.asyncio
async def test_crypto_dashboard_sorts_cards_by_move_then_volume_before_limit():
    markets = [
        _market("KRW-AAA", "AAA", "에이"),
        _market("KRW-BBB", "BBB", "비"),
        _market("KRW-CCC", "CCC", "씨"),
    ]

    async def ticker_provider(requested_markets):
        assert requested_markets == ["KRW-AAA", "KRW-BBB", "KRW-CCC"]
        return [
            {
                "market": "KRW-AAA",
                "trade_price": 1000,
                "signed_change_rate": 0.01,
                "acc_trade_price_24h": 10_000_000_000,
            },
            {
                "market": "KRW-BBB",
                "trade_price": 1000,
                "signed_change_rate": -0.07,
                "acc_trade_price_24h": 100_000_000,
            },
            {
                "market": "KRW-CCC",
                "trade_price": 1000,
                "signed_change_rate": 0.07,
                "acc_trade_price_24h": 200_000_000,
            },
        ]

    async def spread_provider(requested_markets):
        assert requested_markets == ["KRW-CCC", "KRW-BBB"]
        return dict.fromkeys(requested_markets, 0.2)

    response = await build_crypto_dashboard(
        db=FakeSession(markets, []),
        user_id=7,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
        limit=2,
        orderbook_limit=2,
    )

    assert [card.symbol for card in response.cards] == ["KRW-CCC", "KRW-BBB"]
    assert "KRW-AAA" not in [card.symbol for card in response.cards]


@pytest.mark.asyncio
async def test_crypto_dashboard_ranks_full_universe_before_limit():
    markets = [_market(f"KRW-{index:03d}", f"C{index:03d}", f"코인{index:03d}") for index in range(60)]
    top_market = markets[-1].market

    async def ticker_provider(requested_markets):
        assert requested_markets == [market.market for market in markets]
        return [
            {
                "market": market.market,
                "trade_price": 1000,
                "signed_change_rate": 0.2 if market.market == top_market else 0.01,
                "acc_trade_price_24h": 10_000_000_000,
            }
            for market in markets
        ]

    async def spread_provider(requested_markets):
        assert requested_markets == [top_market]
        return {top_market: 0.2}

    response = await build_crypto_dashboard(
        db=FakeSession(markets, []),
        user_id=7,
        ticker_provider=ticker_provider,
        orderbook_spread_provider=spread_provider,
        limit=1,
        orderbook_limit=1,
    )

    assert [card.symbol for card in response.cards] == [top_market]


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
    assert response.cards[0].risk is not None
    assert response.cards[0].risk.level == "unknown"
    assert "data_unavailable" in {badge.kind for badge in response.cards[0].badges}
    assert response.insights.candidates == []
    assert response.pendingOrders is not None
    assert response.pendingOrders.emptyState == "no_pending_orders"
    assert "crypto_ticker_unavailable" in response.meta.warnings
    assert "crypto_orderbook_unavailable" in response.meta.warnings
    assert {source.source for source in response.meta.sources} >= {
        "upbit_ticker",
        "pending_orders",
        "mcp_risk_reference",
        "mcp_candidate_reference",
    }
    states = {source.source: source.state for source in response.meta.sources}
    assert states["upbit_ticker"] == "unavailable"
    assert states["pending_orders"] == "supported"
    assert states["mcp_risk_reference"] == "reference_only"


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
    from app.services.upbit_public_read_model import close_default_read_model

    class FakeRedis:
        def __init__(self):
            self.values: dict[str, str] = {}
            self.closed = False

        async def get(self, key: str):
            return self.values.get(key)

        async def set(self, key: str, value: str, *, ex: int | None = None):
            self.values[key] = value

        async def aclose(self):
            self.closed = True

    await close_default_read_model()
    redis_clients = []

    async def create_redis_client():
        client = FakeRedis()
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
