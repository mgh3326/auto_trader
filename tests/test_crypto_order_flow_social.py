"""ROB-452 P2: get_crypto_order_flow + get_crypto_social handlers (hermetic)."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals import _crypto as mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------- order_flow ----------------


def _tick(ask_bid, volume, timestamp=None):
    res = {"ask_bid": ask_bid, "trade_volume": volume, "trade_price": 100.0}
    if timestamp is not None:
        res["timestamp"] = timestamp
    return res


async def test_order_flow_volume_weighted(monkeypatch):
    captured = {}

    async def fake_trades(market="KRW-BTC", count=500):
        captured["market"] = market
        captured["count"] = count
        return [
            _tick("BID", 20, 1600000020000),
            _tick("BID", 10, 1600000010000),
            _tick("ASK", 10, 1600000000000),
        ]

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc", count=200)

    assert out["source"] == "upbit"
    assert captured["market"] == "KRW-BTC"
    assert captured["count"] == 500  # Always 500 count fetched
    assert out["taker_buy_ratio"] == pytest.approx(0.75)
    assert out["taker_sell_ratio"] == pytest.approx(0.25)
    assert out["net"] == pytest.approx(0.5)
    assert out["trade_count"] == 3
    assert out["default_window"] == 200

    # Verify windows structure
    assert "50" in out["windows"]
    assert "200" in out["windows"]
    assert "500" in out["windows"]

    assert out["windows"]["50"]["net"] == pytest.approx(0.5)
    assert out["windows"]["50"]["trade_count"] == 3
    assert out["windows"]["50"]["span_seconds"] == pytest.approx(20.0)

    # Consensus
    assert out["consensus"]["direction"] == "buy"
    assert out["consensus"]["agreement"] is True
    assert out["consensus"]["confidence"] == "low"  # count < 15


async def test_order_flow_empty_is_none(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        return []

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("KRW-XRP")
    assert out["taker_buy_ratio"] is None
    assert out["net"] is None
    assert out["trade_count"] == 0
    assert out["windows"]["50"]["net"] is None


async def test_order_flow_fail_open(monkeypatch):
    async def boom(market="KRW-BTC", count=500):
        raise RuntimeError("upbit down")

    monkeypatch.setattr(mod, "fetch_recent_trades", boom)
    out = await mod.handle_get_crypto_order_flow("BTC")
    assert "error" in out
    assert out["source"] == "upbit"
    assert "binance" not in str(out.get("source"))


async def test_order_flow_deadband(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        return [_tick("BID", 10.5, 10000000), _tick("ASK", 9.5, 9000000)]

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    assert out["net"] == pytest.approx(0.05)
    assert out["consensus"]["direction"] == "neutral"
    assert out["consensus"]["agreement"] is True
    assert out["consensus"]["trend"] == "neutral"


async def test_order_flow_whale_guard(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        ticks = [_tick("BID", 40.0, 2000000)]
        for i in range(19):
            ticks.append(_tick("ASK", 3.0, 1000000 - i * 1000))
        return ticks

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    assert out["windows"]["50"]["trade_count"] == 20
    assert out["windows"]["50"]["largest_trade_share"] == 0.4124
    assert out["consensus"]["confidence"] == "low"
    assert "Whale trade dominance" in out["consensus"]["note"]


async def test_order_flow_disjoint_trend(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        ticks = []
        for i in range(50):
            ticks.append(_tick("BID", 1.0, 2000000 - i * 1000))
        for i in range(10):
            ticks.append(_tick("ASK", 10.0, 1000000 - i * 1000))
        return ticks

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    assert out["consensus"]["trend"] == "reversing_up"


async def test_order_flow_disjoint_trend_strengthening(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        ticks = []
        for _ in range(40):
            ticks.append(_tick("BID", 2.0, 2000000))
        for _ in range(10):
            ticks.append(_tick("ASK", 2.0, 2000000))
        for _ in range(20):
            ticks.append(_tick("BID", 2.0, 1000000))
        for _ in range(20):
            ticks.append(_tick("ASK", 2.0, 1000000))
        return ticks

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    assert out["consensus"]["trend"] == "strengthening_up"


async def test_order_flow_window_derivation(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=500):
        ticks = []
        for _ in range(50):
            ticks.append(_tick("BID", 1.0, 3000000))
        for _ in range(150):
            ticks.append(_tick("ASK", 1.0, 2000000))
        for _ in range(300):
            ticks.append(_tick("BID", 1.0, 1000000))
        return ticks

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    assert out["windows"]["50"]["net"] == pytest.approx(1.0)
    assert out["windows"]["200"]["net"] == pytest.approx(-0.5)
    assert out["windows"]["500"]["net"] == pytest.approx(0.4)


async def test_order_flow_defensive_sorting(monkeypatch):
    """ROB-589: Ensure handler sorts newest-first even if source is shuffled."""

    async def fake_trades(market="KRW-BTC", count=500):
        # Shuffled input: 1000ms (oldest), 3000ms (newest), 2000ms (middle)
        return [
            _tick("ASK", 1.0, 1000),
            _tick("BID", 1.0, 3000),
            _tick("BID", 1.0, 2000),
        ]

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc")

    # If sorted correctly (3000, 2000, 1000):
    # Whole set (3 trades): 2 BID, 1 ASK -> net = (2-1)/3 = 0.3333
    # Span = (3000 - 1000) / 1000 = 2.0s
    assert out["trade_count"] == 3
    assert out["windows"]["50"]["net"] == pytest.approx(0.3333)
    assert out["windows"]["50"]["span_seconds"] == pytest.approx(2.0)


async def test_order_flow_requires_symbol():
    with pytest.raises(ValueError, match="symbol is required"):
        await mod.handle_get_crypto_order_flow("")


# ---------------- social ----------------


async def test_social_maps_fields(monkeypatch):
    async def fake_resolve(symbol):
        return "bitcoin"

    async def fake_social(coin_id):
        assert coin_id == "bitcoin"
        return {
            "sentiment_votes_up_percentage": 68.5,
            "community_data": {"twitter_followers": 1000, "reddit_subscribers": 500},
            "developer_data": {"commit_count_4_weeks": 42},
        }

    monkeypatch.setattr(mod, "_resolve_coingecko_coin_id", fake_resolve)
    monkeypatch.setattr(mod, "_fetch_coingecko_coin_social", fake_social)
    out = await mod.handle_get_crypto_social("BTC")

    assert out["source"] == "coingecko"
    assert out["coin_id"] == "bitcoin"
    assert out["sentiment_votes_up_pct"] == pytest.approx(68.5)
    assert out["twitter_followers"] == 1000
    assert out["reddit_subscribers"] == 500
    assert out["dev_commits_4w"] == 42


async def test_social_degrades_on_missing_blocks(monkeypatch):
    async def fake_resolve(symbol):
        return "smallcoin"

    async def fake_social(coin_id):
        return {}  # no community/developer blocks (small coin)

    monkeypatch.setattr(mod, "_resolve_coingecko_coin_id", fake_resolve)
    monkeypatch.setattr(mod, "_fetch_coingecko_coin_social", fake_social)
    out = await mod.handle_get_crypto_social("SMALL")
    assert out["twitter_followers"] is None
    assert out["dev_commits_4w"] is None
    assert out["sentiment_votes_up_pct"] is None


async def test_social_fail_open(monkeypatch):
    async def boom(symbol):
        raise RuntimeError("coingecko 429")

    monkeypatch.setattr(mod, "_resolve_coingecko_coin_id", boom)
    out = await mod.handle_get_crypto_social("BTC")
    assert "error" in out
    assert out["source"] == "coingecko"
