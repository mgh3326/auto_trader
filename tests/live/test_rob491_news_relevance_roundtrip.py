"""Integration roundtrip for ROB-491 (Live fetch + DB persist)."""

from __future__ import annotations

import pytest

from app.services import symbol_news_service


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.asyncio
async def test_naver_to_db_roundtrip_integration(db_session) -> None:
    # 1. 실시간 Naver Fetch + DB Upsert (fetch_symbol_news 내부에서 수행)
    symbol = "005930"  # 삼성전자
    result = await symbol_news_service.fetch_symbol_news(symbol, "kr", limit=5)

    assert result.status == "ok"
    assert result.returned_count > 0
    assert not result.degraded

    # 2. 결과물에 relevance 블록이 포함되어 있는지 확인
    art = result.articles[0]
    relevance = art.provider_metadata.get("relevance")
    assert relevance is not None
    assert relevance["status"] == "pending"
    # 삼성전자 기사라면 hints에 alias_match가 있어야 함 (거의 확실)
    if "삼성전자" in art.title:
        assert "삼성전자" in relevance["hints"]["alias_match"]

    # 3. 재호출 시 DB에서 로드되는지 확인 (upsert 멱등성 및 로드 검증)
    # fetch_symbol_news는 매번 Naver를 찌르지만, 결과는 항상 DB canonical state를 따름
    result2 = await symbol_news_service.fetch_symbol_news(symbol, "kr", limit=5)
    assert result2.returned_count >= result.returned_count
    assert result2.articles[0].provider_metadata["relevance"]["status"] == "pending"
