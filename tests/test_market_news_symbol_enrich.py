# tests/test_market_news_symbol_enrich.py
import pytest

from app.mcp_server.tooling import news_handlers as nh
from app.models.news import NewsArticle
from app.services.kr_news_symbol_mapping.contract import CandidateRow


@pytest.mark.unit
def test_compute_mapped_symbols_naver_code_wins_over_ner():
    # stock_symbol confirmed AND title NER-matches the same symbol -> naver_code wins
    article = NewsArticle(
        id=1,
        market="kr",
        title="삼성전자 신규 투자",
        summary=None,
        keywords=None,
        stock_symbol="005930",
    )
    out = nh.compute_mapped_symbols(article, ())
    hit = next(m for m in out if m["symbol"] == "005930")
    assert hit["mapping_source"] == "naver_code"
    assert hit["is_primary"] is True
    assert hit["confidence"] == 1.0


@pytest.mark.unit
def test_compute_mapped_symbols_mainnews_ner_only():
    # mainnews: no stock_symbol, no persisted rows, but title mentions 삼성전자 -> NER maps
    article = NewsArticle(
        id=2,
        market="kr",
        title="삼성전자 사옥 방문",
        summary=None,
        keywords=None,
        stock_symbol=None,
    )
    out = nh.compute_mapped_symbols(article, ())
    hit = next(m for m in out if m["symbol"] == "005930")
    assert hit["mapping_source"] == "ner"


@pytest.mark.unit
def test_compute_mapped_symbols_persisted_candidate_row():
    article = NewsArticle(
        id=3,
        market="kr",
        title="오늘 증시 코멘트",
        summary=None,
        keywords=None,
        stock_symbol=None,
    )
    out = nh.compute_mapped_symbols(
        article, (CandidateRow(symbol="000660", source="candidate", score=0.8),)
    )
    hit = next(m for m in out if m["symbol"] == "000660")
    assert hit["mapping_source"] == "candidate"
    assert hit["confidence"] == 0.8


@pytest.mark.unit
def test_compute_mapped_symbols_empty_when_no_match():
    article = NewsArticle(
        id=4,
        market="kr",
        title="오늘 날씨는 맑음",
        summary=None,
        keywords=None,
        stock_symbol=None,
    )
    assert nh.compute_mapped_symbols(article, ()) == []


@pytest.mark.unit
def test_article_to_dict_includes_mapped_symbols():
    article = NewsArticle(
        id=5,
        market="kr",
        url="https://x/5",
        title="t",
        source="s",
        feed_source="f",
        summary=None,
        article_published_at=None,
        keywords=None,
        stock_symbol=None,
        stock_name=None,
    )
    mapped_by_id = {
        5: [
            {
                "symbol": "035420",
                "market": "kr",
                "mapping_source": "ner",
                "confidence": 0.5,
                "is_primary": True,
                "matched_term": "네이버",
            }
        ]
    }
    item = nh._article_to_dict(article, mapped_by_id=mapped_by_id)
    assert item["mapped_symbols"] == mapped_by_id[5]


@pytest.mark.unit
def test_article_to_dict_mapped_symbols_defaults_empty():
    article = NewsArticle(
        id=6,
        market="kr",
        url="https://x/6",
        title="t",
        source="s",
        feed_source="f",
        summary=None,
        article_published_at=None,
        keywords=None,
        stock_symbol=None,
        stock_name=None,
    )
    item = nh._article_to_dict(article)
    assert item["mapped_symbols"] == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_market_news_enriches_mainnews_via_ner(monkeypatch):
    a1 = NewsArticle(
        id=1,
        market="kr",
        url="https://x/1",
        title="삼성전자 신고가 경신",
        source="조선비즈",
        feed_source="browser_naver_mainnews",
        summary=None,
        article_published_at=None,
        keywords=None,
        stock_symbol=None,
        stock_name=None,
    )

    async def fake_get_news_articles(**kwargs):
        return [a1], 1

    async def fake_loader(article_ids):
        return {}  # mainnews: no persisted related rows

    monkeypatch.setattr(nh, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(nh, "load_related_rows_by_article_ids", fake_loader)

    resp = await nh._get_market_news_impl(market="kr", hours=24, limit=10)
    item = resp["news"][0]
    assert any(
        m["symbol"] == "005930" for m in item["mapped_symbols"]
    )  # NER mapped despite no persisted rows
    # existing fields preserved
    assert item["stock_symbol"] is None


@pytest.mark.asyncio
@pytest.mark.unit
async def test_get_market_news_uses_persisted_related_rows(monkeypatch):
    from app.services.kr_news_symbol_mapping.contract import CandidateRow

    a1 = NewsArticle(
        id=7,
        market="kr",
        url="https://x/7",
        title="증시 코멘트",
        source="s",
        feed_source="f",
        summary=None,
        article_published_at=None,
        keywords=None,
        stock_symbol=None,
        stock_name=None,
    )

    async def fake_get_news_articles(**kwargs):
        return [a1], 1

    async def fake_loader(article_ids):
        return {7: (CandidateRow(symbol="000660", source="candidate", score=0.8),)}

    monkeypatch.setattr(nh, "get_news_articles", fake_get_news_articles)
    monkeypatch.setattr(nh, "load_related_rows_by_article_ids", fake_loader)

    resp = await nh._get_market_news_impl(market="kr", hours=24, limit=10)
    syms = resp["news"][0]["mapped_symbols"]
    assert any(
        m["symbol"] == "000660" and m["mapping_source"] == "candidate" for m in syms
    )
