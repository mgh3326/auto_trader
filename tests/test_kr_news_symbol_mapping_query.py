# tests/test_kr_news_symbol_mapping_query.py
from datetime import UTC, datetime, timedelta

import pytest

from app.services.kr_news_symbol_mapping.contract import ArticleView, CandidateRow
from app.services.kr_news_symbol_mapping.query_service import get_symbol_news_mapping

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_assembles_mapping_for_target_symbol_with_freshness():
    articles = [
        ArticleView(
            market="kr",
            stock_symbol="005930",  # naver_code 확정
            related_rows=(),
            title="삼성전자 신규 투자",
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=1),
        ),
        ArticleView(
            market="kr",
            stock_symbol=None,
            related_rows=(),
            title="네이버 실적 발표",  # NER로 035420 매핑 → target과 무관
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=2),
        ),
    ]

    async def provider(symbol, market, hours, limit):
        return articles

    result = await get_symbol_news_mapping(
        "005930", market="kr", hours=24, limit=20, now=NOW, article_provider=provider
    )

    assert result.symbol == "005930"
    # target symbol(005930)을 매핑한 기사만 포함
    assert len(result.articles) == 1
    primary = result.articles[0].mapped_symbols[0]
    assert primary.symbol == "005930"
    assert primary.mapping_source == "naver_code"
    assert primary.is_primary is True
    assert result.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_no_matching_articles_is_unavailable():
    async def provider(symbol, market, hours, limit):
        return []

    result = await get_symbol_news_mapping(
        "005930", market="kr", now=NOW, article_provider=provider
    )
    assert result.articles == ()
    assert result.freshness.overall == "unavailable"


@pytest.mark.asyncio
async def test_candidate_row_article_maps_target():
    articles = [
        ArticleView(
            market="kr",
            stock_symbol=None,
            related_rows=(
                CandidateRow(
                    symbol="000660", source="news_ingestor", score=0.8, rank=1
                ),
            ),
            title="반도체 업황",
            summary=None,
            keywords=(),
            as_of=NOW - timedelta(hours=3),
        )
    ]

    async def provider(symbol, market, hours, limit):
        return articles

    result = await get_symbol_news_mapping(
        "000660", market="kr", now=NOW, article_provider=provider
    )
    assert len(result.articles) == 1
    m = result.articles[0].mapped_symbols[0]
    assert m.mapping_source == "candidate"
    assert m.confidence == 0.8
