# tests/test_news_symbol_mapping_handler.py
from datetime import UTC, datetime

import pytest

from app.mcp_server.tooling import news_symbol_mapping as nsm
from app.services.kr_news_symbol_mapping.contract import ArticleView

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_returns_mapped_symbols_and_url():
    async def provider(symbol, market, hours, limit):
        return [
            ArticleView(
                market="kr",
                stock_symbol="035420",
                related_rows=(),
                title="네이버 GTC",
                summary="리드",
                keywords=(),
                as_of=NOW,
                url="https://n.news.naver.com/a/1",
            )
        ]

    resp = await nsm.handle_get_symbol_news_mapping(
        symbol="035420", market="kr", now=NOW, article_provider=provider
    )
    assert resp["symbol"] == "035420"
    assert resp["market"] == "kr"
    assert resp["data_state"] == "fresh"
    assert len(resp["articles"]) == 1
    art = resp["articles"][0]
    assert art["url"] == "https://n.news.naver.com/a/1"
    assert art["summary"] == "리드"
    assert art["mapped_symbols"][0]["symbol"] == "035420"
    assert art["mapped_symbols"][0]["mapping_source"] == "naver_code"
    assert art["mapped_symbols"][0]["is_primary"] is True


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_unavailable_is_honest_not_error():
    async def empty_provider(symbol, market, hours, limit):
        return []

    resp = await nsm.handle_get_symbol_news_mapping(
        symbol="999999", market="kr", now=NOW, article_provider=empty_provider
    )
    assert resp["data_state"] == "unavailable"
    assert resp["articles"] == []
    assert any("매핑된 뉴스가 없" in w for w in resp["warnings"])
    assert "error" not in resp
