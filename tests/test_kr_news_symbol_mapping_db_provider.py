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
