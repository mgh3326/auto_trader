"""ROB-452 P2: get_crypto_order_flow + get_crypto_social handlers (hermetic)."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals import _crypto as mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------- order_flow ----------------


def _tick(ask_bid, volume):
    return {"ask_bid": ask_bid, "trade_volume": volume, "trade_price": 100.0}


async def test_order_flow_volume_weighted(monkeypatch):
    captured = {}

    async def fake_trades(market="KRW-BTC", count=50):
        captured["market"] = market
        captured["count"] = count
        # buy vol 30, sell vol 10 → buy 0.75 / sell 0.25 / net 0.5 (volume-weighted,
        # NOT count-weighted: 2 buy ticks vs 1 sell tick would give 0.667 if by count)
        return [_tick("BID", 20), _tick("BID", 10), _tick("ASK", 10)]

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("btc", count=200)

    assert out["source"] == "upbit"  # ROB-285: not binance
    assert captured["market"] == "KRW-BTC"  # symbol normalized
    assert captured["count"] == 200
    assert out["taker_buy_ratio"] == pytest.approx(0.75)
    assert out["taker_sell_ratio"] == pytest.approx(0.25)
    assert out["net"] == pytest.approx(0.5)
    assert out["trade_count"] == 3


async def test_order_flow_empty_is_none(monkeypatch):
    async def fake_trades(market="KRW-BTC", count=50):
        return []

    monkeypatch.setattr(mod, "fetch_recent_trades", fake_trades)
    out = await mod.handle_get_crypto_order_flow("KRW-XRP")
    assert out["taker_buy_ratio"] is None  # missing != zero
    assert out["net"] is None
    assert out["trade_count"] == 0


async def test_order_flow_fail_open(monkeypatch):
    async def boom(market="KRW-BTC", count=50):
        raise RuntimeError("upbit down")

    monkeypatch.setattr(mod, "fetch_recent_trades", boom)
    out = await mod.handle_get_crypto_order_flow("BTC")
    assert "error" in out
    assert out["source"] == "upbit"
    assert "binance" not in str(out.get("source"))


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
