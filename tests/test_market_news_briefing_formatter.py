from __future__ import annotations

from types import SimpleNamespace

import pytest


def _article(
    title: str,
    *,
    market: str,
    summary: str | None = None,
    source: str = "Test Source",
    feed_source: str = "rss_test",
    stock_symbol: str | None = None,
    keywords: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=None,
        title=title,
        url="https://example.com/article",
        source=source,
        feed_source=feed_source,
        market=market,
        summary=summary,
        article_published_at=None,
        keywords=keywords or [],
        stock_symbol=stock_symbol,
        stock_name=None,
    )


@pytest.mark.unit
def test_format_us_news_groups_macro_big_tech_earnings_and_noise():
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    macro = _article(
        "Fed rate cut hopes lift S&P 500 futures before CPI report",
        market="us",
        summary="Inflation data and Treasury yields remain the macro focus.",
    )
    big_tech = _article(
        "Microsoft and Nvidia lead Nasdaq as AI chip demand grows",
        market="us",
        summary="Big tech and semiconductors continue to drive market sentiment.",
        stock_symbol="NVDA",
    )
    earnings = _article(
        "Apple earnings beat estimates but guidance disappoints",
        market="us",
        summary="Quarterly results and outlook are in focus.",
        stock_symbol="AAPL",
    )
    lifestyle = _article(
        "Celebrity mansion sells after renovation show",
        market="us",
        summary="Real estate lifestyle story with no market signal.",
    )

    briefing = format_market_news_briefing(
        [lifestyle, earnings, big_tech, macro], market="us", limit=10
    )

    assert briefing.market == "us"
    assert briefing.summary["included"] == 3
    assert briefing.summary["excluded"] == 1
    section_ids = [section.section_id for section in briefing.sections]
    assert section_ids[:3] == ["macro_fed", "big_tech", "earnings"]
    assert [item.article.title for item in briefing.sections[0].items] == [macro.title]
    assert briefing.excluded[0].relevance.reason == "low_market_relevance"


@pytest.mark.unit
def test_format_us_news_excludes_personal_finance_and_lifestyle_rate_noise():
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    savings_rate = _article(
        "Best high-yield savings interest rates today, April 30, 2026",
        market="us",
        summary="A personal finance roundup of accounts paying 4.1% APY.",
    )
    mortgage_rate = _article(
        "Mortgage rates increase to 6.3% — but home buyers are not scared away",
        market="us",
        summary="Consumer mortgage and home-buying advice rather than market-moving macro.",
    )
    fed_rate = _article(
        "Fed rate cut hopes lift S&P 500 futures before CPI report",
        market="us",
        summary="Inflation data and Treasury yields remain the macro focus.",
    )

    briefing = format_market_news_briefing(
        [savings_rate, mortgage_rate, fed_rate], market="us", limit=10
    )

    assert briefing.summary["included"] == 1
    assert briefing.sections[0].items[0].article.title == fed_rate.title
    assert {item.article.title for item in briefing.excluded} == {
        savings_rate.title,
        mortgage_rate.title,
    }
    assert all(
        item.relevance.reason == "low_market_relevance" for item in briefing.excluded
    )


@pytest.mark.unit
def test_format_kr_news_groups_preopen_sector_disclosure_and_flow():
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    preopen = _article(
        "코스피 장전 주요 뉴스: 환율 하락과 반도체 강세",
        market="kr",
        summary="장전 시황과 지수 흐름이 투자심리에 영향을 준다.",
    )
    sector = _article(
        "2차전지 업종 강세, 배터리 소재 테마 부각",
        market="kr",
        summary="업종/테마 순환매가 나타난다.",
    )
    disclosure = _article(
        "삼성전자 신규 공급계약 공시 발표",
        market="kr",
        summary="기업 공시와 계약 뉴스.",
        stock_symbol="005930",
    )
    flow = _article(
        "외국인 순매수 확대, 기관 수급도 개선",
        market="kr",
        summary="수급 개선과 관심종목 영향.",
    )

    briefing = format_market_news_briefing(
        [flow, disclosure, sector, preopen], market="kr", limit=10
    )

    assert briefing.summary["included"] == 4
    assert [section.section_id for section in briefing.sections] == [
        "preopen_headlines",
        "sector_theme",
        "disclosure_research",
        "flow_watchlist",
    ]


@pytest.mark.unit
def test_format_crypto_news_reuses_crypto_relevance_and_sections():
    from app.services.market_news_briefing_formatter import format_market_news_briefing

    etf = _article(
        "Bitcoin ETF inflows rebound as BTC volatility rises",
        market="crypto",
        feed_source="rss_cointelegraph",
    )
    security = _article(
        "DeFi protocol hack drains exchange wallet funds",
        market="crypto",
        feed_source="rss_coindesk",
    )
    ai_noise = _article(
        "OpenAI launches Linux coding model",
        market="crypto",
        summary="Developer tool story without blockchain or token impact.",
        feed_source="rss_decrypt",
    )

    briefing = format_market_news_briefing(
        [ai_noise, security, etf], market="crypto", limit=10
    )

    assert briefing.summary["included"] == 2
    assert briefing.summary["excluded"] == 1
    assert [section.section_id for section in briefing.sections] == [
        "btc_eth_market",
        "security_defi",
    ]
    assert briefing.sections[0].items[0].article.title == etf.title
    assert briefing.excluded[0].relevance.reason == "broad_tech_without_crypto_signal"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_market_news_briefing_filter_formats_us_sections_for_mcp():
    from unittest.mock import AsyncMock, patch

    from app.mcp_server.tooling.news_handlers import _get_market_news_impl

    rows = [
        _article(
            "Fed rate cut hopes lift S&P 500 futures before CPI report",
            market="us",
        ),
        _article(
            "Celebrity mansion sells after renovation show",
            market="us",
        ),
    ]

    with patch(
        "app.mcp_server.tooling.news_handlers.get_news_articles",
        new=AsyncMock(return_value=(rows, 2)),
    ):
        result = await _get_market_news_impl(
            market="us",
            hours=24,
            limit=10,
            briefing_filter=True,
        )

    assert result["count"] == 1
    assert result["briefing_filter"] is True
    assert result["briefing_summary"]["included"] == 1
    assert result["briefing_sections"][0]["section_id"] == "macro_fed"
    assert (
        result["excluded_news"][0]["briefing_relevance"]["reason"]
        == "low_market_relevance"
    )
