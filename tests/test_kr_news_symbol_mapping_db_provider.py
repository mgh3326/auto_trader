# tests/test_kr_news_symbol_mapping_db_provider.py
from datetime import UTC, datetime

import pytest

from app.models.news import NewsArticle, NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping import db_provider as dbp
from app.services.kr_news_symbol_mapping.contract import CandidateRow

NOW = datetime(2026, 6, 9, 3, 0, tzinfo=UTC)


@pytest.mark.unit
def test_candidate_rows_from_orm_maps_fields():
    rows = [
        NewsArticleRelatedSymbol(
            article_id=1,
            market="kr",
            symbol="035420",
            source="naver_code",
            matched_term=None,
            score=None,
            rank=1,
        ),
        NewsArticleRelatedSymbol(
            article_id=1,
            market="kr",
            symbol="000660",
            source="ner",
            matched_term="닉스",
            score=0.5,
            rank=2,
        ),
    ]
    out = dbp._candidate_rows_from_orm(rows)
    assert out == (
        CandidateRow(
            symbol="035420", source="naver_code", score=None, rank=1, matched_term=None
        ),
        CandidateRow(
            symbol="000660", source="ner", score=0.5, rank=2, matched_term="닉스"
        ),
    )


@pytest.mark.unit
def test_article_to_view_maps_fields_and_url():
    article = NewsArticle(
        id=7,
        market="kr",
        url="https://n.news.naver.com/a/1",
        title="네이버 GTC 언급",
        summary="리드",
        keywords=["AI"],
        stock_symbol="035420",
        article_published_at=NOW,
        scraped_at=NOW,
    )
    related = (CandidateRow(symbol="035420", source="naver_code"),)
    view = dbp._article_to_view(article, related)
    assert view.market == "kr"
    assert view.stock_symbol == "035420"
    assert view.related_rows == related
    assert view.title == "네이버 GTC 언급"
    assert view.summary == "리드"
    assert view.keywords == ("AI",)
    assert view.as_of == NOW
    assert view.url == "https://n.news.naver.com/a/1"


@pytest.mark.unit
def test_article_to_view_as_of_falls_back_to_scraped_at():
    article = NewsArticle(
        id=8,
        market="kr",
        url="https://x/2",
        title="t",
        summary=None,
        keywords=None,
        stock_symbol=None,
        article_published_at=None,
        scraped_at=NOW,
    )
    view = dbp._article_to_view(article, ())
    assert view.as_of == NOW  # published_at None -> scraped_at
    assert view.keywords == ()  # None -> ()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_db_article_provider_builds_views(monkeypatch):
    from app.services.llm_news_service import NewsLookupResult

    a1 = NewsArticle(
        id=1,
        market="kr",
        url="https://x/1",
        title="삼성",
        summary=None,
        keywords=["반도체"],
        stock_symbol="005930",
        article_published_at=NOW,
        scraped_at=NOW,
    )
    a2 = NewsArticle(
        id=2,
        market="kr",
        url="https://x/2",
        title="네이버",
        summary="리드",
        keywords=None,
        stock_symbol=None,
        article_published_at=None,
        scraped_at=NOW,
    )

    async def fake_fallback(*, symbol, market, hours, limit):
        return NewsLookupResult(articles=[a1, a2], match_reasons={})

    async def fake_load_related(article_ids):
        assert set(article_ids) == {1, 2}
        return {2: (CandidateRow(symbol="035420", source="naver_code"),)}

    monkeypatch.setattr(dbp, "get_news_articles_with_fallback", fake_fallback)
    monkeypatch.setattr(dbp, "_load_related_rows", fake_load_related)

    views = await dbp.db_article_provider("005930", "kr", 24, 20)
    assert [v.url for v in views] == ["https://x/1", "https://x/2"]
    assert views[0].related_rows == ()  # a1 had no related rows
    assert views[1].related_rows == (
        CandidateRow(symbol="035420", source="naver_code"),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_db_article_provider_empty_when_no_articles(monkeypatch):
    from app.services.llm_news_service import NewsLookupResult

    async def fake_fallback(*, symbol, market, hours, limit):
        return NewsLookupResult(articles=[], match_reasons={})

    monkeypatch.setattr(dbp, "get_news_articles_with_fallback", fake_fallback)
    views = await dbp.db_article_provider("999999", "kr", 24, 20)
    assert views == []
