"""ROB-502 step 5: quality-gated MCP exposure for market news/issues.

Covers the ported title-noise classifier, the noise pre-gate + word-boundary
term matching in the briefing formatter, and the meaningfulness thresholds in
the issue clustering service (single-source exclusion, near-duplicate merge,
empty-with-reason responses).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.timezone import now_kst_naive


def _article(
    title: str,
    *,
    market: str,
    summary: str | None = None,
    source: str = "Test Source",
    feed_source: str = "rss_test",
    stock_symbol: str | None = None,
    keywords: list[str] | None = None,
    article_id: int | None = None,
    published_minutes_ago: int = 30,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=article_id,
        title=title,
        url=f"https://example.com/{article_id or 'article'}",
        source=source,
        feed_source=feed_source,
        market=market,
        summary=summary,
        article_published_at=now_kst_naive() - timedelta(minutes=published_minutes_ago),
        keywords=keywords or [],
        stock_symbol=stock_symbol,
        stock_name=None,
    )


# ---------------------------------------------------------------------------
# Noise classifier (ported from news-ingestor noise.py)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_noise_classifier_categories():
    from app.services.market_news_noise import classify_title_noise

    assert "personal_finance" in classify_title_noise(
        "My plumber charged $160 to fix a problem — do I pay again?"
    )
    assert "lifestyle" in classify_title_noise(
        "Buyer swoops in for actress Dakota Johnson's $6 million midcentury home"
    )
    assert "price_prediction" in classify_title_noise(
        "XRP Price Prediction: Could XRP reach $10 by 2027?"
    )
    assert "sponsored" in classify_title_noise("Sponsored: The 5 best coins to buy now")
    assert "broad_tech" in classify_title_noise(
        "EU Orders Meta to Open WhatsApp to Rival AI Chatbots"
    )


@pytest.mark.unit
def test_noise_classifier_clean_titles():
    from app.services.market_news_noise import classify_title_noise

    assert (
        classify_title_noise("S&P 500 closes higher as investors await Fed minutes")
        == []
    )
    assert classify_title_noise("'스페이스X' 청약 경쟁률 4배 돌파…역대 최대 IPO") == []
    assert classify_title_noise("") == []


# ---------------------------------------------------------------------------
# Briefing formatter: noise pre-gate + word-boundary matching
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plumber_story_is_noise_gated_not_big_tech():
    """Regression for the 2026-06-10 live finding: the MarketWatch Moneyist
    story landed in Big Tech because the bare term "ai" substring-matched
    "again". It must now be excluded with an explicit noise reason."""
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    plumber = _article(
        "My plumber charged $160 to fix a problem in my bathroom — but appears "
        "to have created another one. Do I pay again?",
        market="us",
        feed_source="rss_marketwatch_topstories",
    )
    briefing = format_market_news_briefing([plumber], market="us", limit=10)
    assert briefing.summary["included"] == 0
    [excluded] = briefing.excluded
    assert excluded.relevance.reason is not None
    assert excluded.relevance.reason.startswith("noise:")
    assert "personal_finance" in excluded.relevance.reason


@pytest.mark.unit
def test_short_terms_require_word_boundaries():
    """A title containing "again"/"aims" must not match the "ai" term; a real
    AI/semis title still lands in big_tech."""
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    not_ai = _article(
        "Boeing aims to deliver again as supply chain improves",
        market="us",
    )
    real_ai = _article(
        "Nvidia AI chip demand lifts Nasdaq semiconductor stocks",
        market="us",
    )
    briefing = format_market_news_briefing([not_ai, real_ai], market="us", limit=10)
    by_title = {
        item.article.title: section.section_id
        for section in briefing.sections
        for item in section.items
    }
    assert by_title.get(real_ai.title) == "big_tech"
    assert not_ai.title not in by_title


@pytest.mark.unit
def test_federal_reserve_still_reaches_macro_fed():
    """Word-boundary matching must not lose "Federal Reserve ..." headlines
    that previously rode the "fed"-in-"federal" substring."""
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    fed = _article(
        "Federal Reserve announces annual bank stress test results",
        market="us",
        feed_source="rss_fed_press",
    )
    briefing = format_market_news_briefing([fed], market="us", limit=10)
    section_ids = [s.section_id for s in briefing.sections]
    assert "macro_fed" in section_ids or "finance_credit_rates" in section_ids


@pytest.mark.unit
def test_crypto_briefing_also_noise_gated():
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    spam = _article(
        "Toncoin (TON) Price Prediction 2025, 2026, 2027-2030",
        market="crypto",
        feed_source="rss_cryptopotato",
    )
    briefing = format_market_news_briefing([spam], market="crypto", limit=10)
    assert briefing.summary["included"] == 0
    [excluded] = briefing.excluded
    assert excluded.relevance.reason.startswith("noise:")


# ---------------------------------------------------------------------------
# Issue clustering: meaningfulness thresholds
# ---------------------------------------------------------------------------


def _build_issues(monkeypatch, articles, **kwargs):
    import asyncio

    from app.services import news_issue_clustering_service as svc

    async def fake_load(*, market, window_hours, max_rows):
        return articles

    monkeypatch.setattr(svc, "_load_recent_articles", fake_load)
    return asyncio.run(svc.build_market_issues(market="us", **kwargs))


@pytest.mark.unit
def test_single_source_single_article_clusters_excluded(monkeypatch):
    """The 2026-06-10 live finding: ranks 3-5 of US issues were single-article
    clusters (SoftBank single, plumber, Dakota Johnson). Thin clusters must
    not be exposed as market issues."""
    arts = [
        _article(
            "Quantum widgets startup raises huge round", market="us", article_id=1
        ),
        _article(
            "Nvidia data center revenue beats expectations",
            market="us",
            source="CNBC",
            article_id=2,
        ),
        _article(
            "Nvidia shares climb on supply commitments",
            market="us",
            source="MarketWatch",
            article_id=3,
        ),
    ]
    resp = _build_issues(monkeypatch, arts)
    titles = [i.issue_title for i in resp.items]
    assert any("Nvidia" in t for t in titles)
    assert not any("Quantum widgets" in t for t in titles)
    assert resp.status == "ok"
    assert resp.quality_gate is not None
    assert resp.quality_gate.clusters_excluded_thin >= 1


@pytest.mark.unit
def test_noise_articles_never_reach_clustering(monkeypatch):
    arts = [
        _article(
            "My plumber charged $160 to fix a problem — do I pay again?",
            market="us",
            source="MarketWatch",
            article_id=1,
        ),
        _article(
            "My plumber story syndicated copy — do I pay again?",
            market="us",
            source="Yahoo",
            article_id=2,
        ),
    ]
    resp = _build_issues(monkeypatch, arts)
    assert resp.items == []
    assert resp.status == "no_meaningful_items"
    assert resp.quality_gate.noise_articles_excluded == 2


@pytest.mark.unit
def test_near_duplicate_clusters_merge(monkeypatch):
    """The 2026-06-10 crypto finding: the same Japan-stablecoin story from two
    outlets appeared as two separate single-article clusters. Near-duplicate
    title clusters must merge — and the merged cluster (2 sources) passes."""
    arts = [
        _article(
            "Japan's Largest Banks Plan Joint Stablecoin Launch by March 2027",
            market="us",
            source="Decrypt",
            article_id=1,
        ),
        _article(
            "Japan's three largest banks aim for joint stablecoin issue by March",
            market="us",
            source="CoinDesk",
            article_id=2,
        ),
    ]
    resp = _build_issues(monkeypatch, arts)
    assert len(resp.items) == 1
    issue = resp.items[0]
    assert issue.article_count == 2
    assert issue.source_count == 2
    assert resp.quality_gate.clusters_merged >= 1


@pytest.mark.unit
def test_important_feed_source_single_article_survives(monkeypatch):
    """Official-source items (Fed press) are meaningful even as singletons."""
    arts = [
        _article(
            "Federal Reserve Board announces bank stress test results date",
            market="us",
            feed_source="rss_fed_press",
            source="Federal Reserve",
            article_id=1,
        ),
    ]
    resp = _build_issues(monkeypatch, arts)
    assert len(resp.items) == 1
    assert resp.status == "ok"


@pytest.mark.unit
def test_empty_window_reports_no_recent_articles(monkeypatch):
    resp = _build_issues(monkeypatch, [])
    assert resp.items == []
    assert resp.status == "no_recent_articles"
    assert resp.degraded_reason


@pytest.mark.unit
def test_all_thin_reports_no_meaningful_items_with_reason(monkeypatch):
    arts = [
        _article("Unique story one about widgets", market="us", article_id=1),
        _article("Totally different tale regarding gadgets", market="us", article_id=2),
    ]
    resp = _build_issues(monkeypatch, arts)
    assert resp.items == []
    assert resp.status == "no_meaningful_items"
    assert resp.degraded_reason
