"""ROB-449: get_retail_sentiment handler + Naver discussion fetcher (hermetic)."""

from __future__ import annotations

import pytest

from app.mcp_server.tooling.fundamentals import _retail_sentiment as mod
from app.services.naver_finance import discussion as disc

pytestmark = [pytest.mark.unit]


# ---------------- fetcher ----------------


def test_extract_ranked_items_defensive():
    payload = {
        "totalCount": 2,
        "rankings": [
            {
                "itemCode": "005930",
                "rank": 1,
                "postCount": 128,
                "commentCount": 342,
                "reactionCount": 911,
                "postTitle": "SHOULD BE IGNORED",
            },
            {"code": "000660", "postCount": 50},  # rank derived from index
        ],
    }
    items = disc._extract_ranked_items(payload)
    assert items[0]["code"] == "005930"
    assert items[0]["rank"] == 1
    assert items[0]["post_count"] == 128
    # aggregate-only: no raw text keys leak into the extracted item
    assert "postTitle" not in items[0] and "title" not in items[0]
    assert items[1]["code"] == "000660"
    assert items[1]["rank"] == 2  # index-derived


@pytest.mark.asyncio
async def test_fetch_rankings_fail_open():
    async def boom():
        raise RuntimeError("naver down")

    out = await disc.fetch_discussion_rankings(size=20, fetcher=boom)
    assert out["state"] == "unavailable"
    assert out["items"] == []
    assert "naver down" in out["errorReason"]


# ---------------- handler ----------------


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(mod.settings, "retail_sentiment_live_enabled", False)
    out = await mod.handle_get_retail_sentiment("005930")
    assert out["status"] == "disabled"
    assert out["source"] == "naver_discussion"


@pytest.mark.asyncio
async def test_ranked_symbol_returns_counts_and_overheat(monkeypatch):
    monkeypatch.setattr(mod.settings, "retail_sentiment_live_enabled", True)

    async def fake_rankings(size=20):
        return {
            "state": "fresh",
            "source": "naver_discussion",
            "fetched_at": "2026-06-09T00:00:00+00:00",
            "items": [
                {
                    "code": "005930",
                    "rank": 2,
                    "post_count": 128,
                    "comment_count": 342,
                    "reaction_count": 911,
                },
            ],
        }

    monkeypatch.setattr(mod, "fetch_discussion_rankings", fake_rankings)
    out = await mod.handle_get_retail_sentiment("005930")
    assert out["status"] == "ok"
    assert out["activity_rank"] == 2
    assert out["post_count"] == 128
    assert out["overheat_flag"] is True  # rank 2 <= 5
    # deferred fields not fabricated
    assert "bull_bear_lean" not in out
    assert "top_themes" not in out


@pytest.mark.asyncio
async def test_not_ranked_is_not_zero(monkeypatch):
    monkeypatch.setattr(mod.settings, "retail_sentiment_live_enabled", True)

    async def fake_rankings(size=20):
        return {"state": "fresh", "items": [{"code": "005930", "rank": 1}]}

    monkeypatch.setattr(mod, "fetch_discussion_rankings", fake_rankings)
    out = await mod.handle_get_retail_sentiment("000660")  # not in ranking
    assert out["status"] == "not_ranked"
    assert out["activity_rank"] is None  # missing != zero
    assert out["overheat_flag"] is False
    assert "post_count" not in out


@pytest.mark.asyncio
async def test_unavailable_when_fetch_degrades(monkeypatch):
    monkeypatch.setattr(mod.settings, "retail_sentiment_live_enabled", True)

    async def fake_rankings(size=20):
        return {"state": "unavailable", "items": []}

    monkeypatch.setattr(mod, "fetch_discussion_rankings", fake_rankings)
    out = await mod.handle_get_retail_sentiment("005930")
    assert out["status"] == "unavailable"


@pytest.mark.asyncio
async def test_kr_only(monkeypatch):
    monkeypatch.setattr(mod.settings, "retail_sentiment_live_enabled", True)
    with pytest.raises(ValueError, match="Korean stocks"):
        await mod.handle_get_retail_sentiment("AAPL")
