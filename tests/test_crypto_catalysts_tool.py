"""ROB-452 P1: Upbit notices fetcher + get_crypto_catalysts handler (hermetic)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.mcp_server.tooling.fundamentals import _crypto_catalysts as mod
from app.services.upbit_public_read_model import notices as notices_mod

pytestmark = [pytest.mark.unit]  # async tests carry @pytest.mark.asyncio individually


# ---------------- notices fetcher ----------------


def test_extract_items_defensive_shapes():
    today = "2026-06-08T00:00:00Z"
    a = {"data": {"notice": [{"title": "x", "listed_at": today}]}}
    b = {"data": {"list": [{"title": "y"}]}}
    c = [{"title": "z"}]
    live = {
        "success": True,
        "data": {
            "fixed_notices": [{"title": "fixed", "listed_at": today}],
            "notices": [{"title": "normal", "listed_at": today}],
        },
    }
    assert notices_mod._extract_items(a)[0]["title"] == "x"
    assert notices_mod._extract_items(b)[0]["title"] == "y"
    assert notices_mod._extract_items(c)[0]["title"] == "z"
    assert [i["title"] for i in notices_mod._extract_items(live)] == [
        "fixed",
        "normal",
    ]
    assert notices_mod._extract_items("garbage") == []


@pytest.mark.asyncio
async def test_fetch_upbit_notices_success_windowed():
    recent = dt.datetime.now(dt.UTC).isoformat()
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=60)).isoformat()

    async def fake():
        return {
            "success": True,
            "data": {
                "notices": [
                    {
                        "id": 1,
                        "title": "BTC 입출금",
                        "category": "입출금",
                        "listed_at": recent,
                        "first_listed_at": recent,
                        "need_new_badge": False,
                    },
                    {
                        "id": 2,
                        "title": "old notice",
                        "category": "general",
                        "listed_at": old,
                    },
                ]
            },
        }

    out = await notices_mod.fetch_upbit_notices(days=14, fetcher=fake)
    assert out["state"] == "fresh"
    titles = [i["title"] for i in out["items"]]
    assert "BTC 입출금" in titles
    assert "old notice" not in titles  # outside 14-day window
    recent_item = next(i for i in out["items"] if i["title"] == "BTC 입출금")
    assert recent_item["id"] == 1
    assert recent_item["category"] == "입출금"
    assert recent_item["need_new_badge"] is False


@pytest.mark.asyncio
async def test_fetch_upbit_notices_fail_open():
    async def boom():
        raise RuntimeError("endpoint down")

    out = await notices_mod.fetch_upbit_notices(days=14, fetcher=boom)
    assert out["state"] == "unavailable"
    assert out["items"] == []
    assert "endpoint down" in out["errorReason"]


# ---------------- catalysts handler ----------------


def _patch_sources(monkeypatch, *, tokenomist, notices, warnings):
    monkeypatch.setattr(mod, "fetch_tokenomist_unlocks_poc", tokenomist)
    monkeypatch.setattr(mod, "tokenomist_api_key_from_env", lambda: None)
    monkeypatch.setattr(mod, "fetch_upbit_notices", notices)
    monkeypatch.setattr(mod, "get_market_warnings", warnings)


@pytest.mark.asyncio
async def test_catalysts_aggregates_three_blocks(monkeypatch):
    async def tokenomist(*, api_key=None):
        return SimpleNamespace(
            metrics=(), warnings=("tokenomist: disabled (missing API key)",)
        )

    async def notices(*, days=14, **kw):
        return {
            "state": "fresh",
            "source": "upbit_notices",
            "items": [
                {"title": "XRP 유의 안내", "category": "general", "listed_at": None}
            ],
        }

    async def warnings(*, markets=None, include_event_detail=False):
        return SimpleNamespace(
            meta=SimpleNamespace(state="fresh", fetchedAt=None),
            entries={
                "KRW-XRP": SimpleNamespace(warning="CAUTION", event={"k": 1}),
                "KRW-BTC": SimpleNamespace(warning="NONE", event=None),
            },
        )

    _patch_sources(
        monkeypatch, tokenomist=tokenomist, notices=notices, warnings=warnings
    )
    out = await mod.handle_get_crypto_catalysts(symbol=None, days=14)

    cat = out["catalysts"]
    # tokenomist PoC → honest "disabled"
    assert cat["token_unlocks"]["state"] == "disabled"
    assert cat["upbit_notices"]["state"] == "fresh"
    # only CAUTION surfaces (NONE filtered out)
    assert set(cat["market_warnings"]["entries"]) == {"KRW-XRP"}


@pytest.mark.asyncio
async def test_catalysts_symbol_scopes_notices(monkeypatch):
    async def tokenomist(*, api_key=None):
        return SimpleNamespace(metrics=(), warnings=())

    async def notices(*, days=14, **kw):
        return {
            "state": "fresh",
            "source": "upbit_notices",
            "items": [
                {"title": "XRP 입출금 재개", "category": "general", "listed_at": None},
                {"title": "ETH 점검 안내", "category": "general", "listed_at": None},
            ],
        }

    async def warnings(*, markets=None, include_event_detail=False):
        return SimpleNamespace(
            meta=SimpleNamespace(state="fresh", fetchedAt=None), entries={}
        )

    _patch_sources(
        monkeypatch, tokenomist=tokenomist, notices=notices, warnings=warnings
    )
    out = await mod.handle_get_crypto_catalysts(symbol="XRP", days=14)

    titles = [i["title"] for i in out["catalysts"]["upbit_notices"]["items"]]
    assert any("XRP" in t for t in titles)
    assert not any("ETH" in t for t in titles)  # scoped to XRP
    assert out["symbol"] == "KRW-XRP"


@pytest.mark.asyncio
async def test_catalysts_fail_open_per_source(monkeypatch):
    async def tokenomist(*, api_key=None):
        raise RuntimeError("tok boom")

    async def notices(*, days=14, **kw):
        return {
            "state": "unavailable",
            "source": "upbit_notices",
            "items": [],
            "errorReason": "n down",
        }

    async def warnings(*, markets=None, include_event_detail=False):
        raise RuntimeError("warn boom")

    _patch_sources(
        monkeypatch, tokenomist=tokenomist, notices=notices, warnings=warnings
    )
    out = await mod.handle_get_crypto_catalysts()  # must not raise

    cat = out["catalysts"]
    assert cat["token_unlocks"]["state"] == "unavailable"
    assert cat["upbit_notices"]["state"] == "unavailable"
    assert cat["market_warnings"]["state"] == "unavailable"
