# tests/test_kr_news_symbol_mapping_related_lookup.py
import pytest

from app.models.news import NewsArticleRelatedSymbol
from app.services.kr_news_symbol_mapping import related_lookup as rl
from app.services.kr_news_symbol_mapping.contract import CandidateRow


@pytest.mark.unit
def test_group_rows_groups_by_article_id_to_candidate_rows():
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
        NewsArticleRelatedSymbol(
            article_id=2,
            market="kr",
            symbol="005930",
            source="candidate",
            matched_term=None,
            score=0.8,
            rank=1,
        ),
    ]
    out = rl._group_rows(rows)
    assert out[1] == (
        CandidateRow(
            symbol="035420", source="naver_code", score=None, rank=1, matched_term=None
        ),
        CandidateRow(
            symbol="000660", source="ner", score=0.5, rank=2, matched_term="닉스"
        ),
    )
    assert out[2] == (
        CandidateRow(
            symbol="005930", source="candidate", score=0.8, rank=1, matched_term=None
        ),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_returns_empty_for_no_ids():
    assert await rl.load_related_rows_by_article_ids([]) == {}
